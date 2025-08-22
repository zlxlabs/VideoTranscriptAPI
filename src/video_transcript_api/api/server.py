import os
import json
import uvicorn
import asyncio
import concurrent.futures
import datetime
import re
import threading
import queue
from fastapi import FastAPI, HTTPException, Depends, Header, BackgroundTasks, Request
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from fastapi.middleware.cors import CORSMiddleware
from typing import Optional, Dict, Any

from ..utils import setup_logger, load_config, WechatNotifier, MetadataCache, CacheManager
from ..utils.user_manager import get_user_manager
from ..utils.audit_logger import get_audit_logger
from ..utils.webhook_rate_limiter import get_rate_limiter_stats, get_webhook_status
from ..utils.markdown_renderer import render_markdown_to_html, get_base_url
from ..utils.dialog_renderer import render_transcript_content, render_transcript_content_smart, render_calibrated_content_smart
from ..utils.timezone_helper import format_datetime_for_display
from ..utils.llm_enhanced import EnhancedLLMProcessor
from ..downloaders import create_downloader
from ..transcriber import Transcriber, FunASRSpeakerClient

# 创建日志记录器
logger = setup_logger("api_server")

# 创建API应用
app = FastAPI(
    title="VideoTranscriptAPI",
    description="视频转录API服务",
    version="1.0.0"
)

# 添加CORS中间件
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 加载配置信息
config = load_config()

# 初始化用户管理器（传入回退配置）
user_manager = get_user_manager(fallback_config=config)

# 初始化审计日志记录器
audit_logger = get_audit_logger()

# 创建转录任务线程池
max_workers = config.get("concurrent", {}).get("max_workers", 3)
executor = concurrent.futures.ThreadPoolExecutor(max_workers=max_workers)

# 创建企业微信通知器
wechat_notifier = WechatNotifier()

# 创建元数据缓存管理器
metadata_cache = MetadataCache()

# 创建新的缓存管理器
cache_dir = config.get("storage", {}).get("cache_dir", "./data/cache")
cache_manager = CacheManager(cache_dir)

# 创建增强LLM处理器
enhanced_llm_processor = EnhancedLLMProcessor(config)

# 创建模板引擎
# 获取模板目录路径
template_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "web", "templates")
templates = Jinja2Templates(directory=template_dir)

# 配置静态文件服务
static_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "web", "static")
app.mount("/static", StaticFiles(directory=static_dir), name="static")

# 任务队列
task_queue = asyncio.Queue(config.get("concurrent", {}).get("queue_size", 10))

# LLM处理队列，使用线程安全的队列，确保同一视频的校对和总结连续发送
llm_task_queue = queue.Queue(maxsize=100)

# 任务结果存储
task_results = {}

# LLM处理锁，确保同一时间只有一个视频在进行LLM处理和微信发送
llm_processing_lock = threading.Lock()


class TranscribeRequest(BaseModel):
    """转录请求数据模型"""
    url: str = Field(..., description="视频URL")
    use_speaker_recognition: bool = Field(False, description="是否使用说话人识别功能")
    wechat_webhook: Optional[str] = Field(None, description="企业微信webhook地址，用于发送通知")


class TranscribeResponse(BaseModel):
    """转录响应数据模型"""
    code: int = Field(200, description="状态码")
    message: str = Field("success", description="状态信息")
    data: Optional[Dict[str, Any]] = Field(None, description="响应数据")


async def verify_token(authorization: str = Header(None), request: Request = None):
    """
    验证API令牌（支持多用户）
    
    Args:
        authorization: Authorization头
        request: FastAPI请求对象
        
    Returns:
        dict: 用户信息
    """
    if not authorization:
        logger.warning("请求未提供Authorization头")
        raise HTTPException(status_code=401, detail="未提供授权令牌")
    
    # 检查令牌格式
    token_parts = authorization.split()
    if len(token_parts) != 2 or token_parts[0].lower() != "bearer":
        logger.warning("授权令牌格式错误")
        raise HTTPException(status_code=401, detail="授权令牌格式错误")
    
    token = token_parts[1]
    
    # 使用用户管理器验证令牌
    user_info = user_manager.validate_token(token)
    if not user_info:
        logger.warning(f"授权令牌无效: {token[:8]}...")
        raise HTTPException(status_code=401, detail="授权令牌无效")
    
    logger.debug(f"用户认证成功: {user_info.get('user_id')}")
    
    # 将用户信息添加到请求状态中（供后续使用）
    if request:
        request.state.user_info = user_info
    
    return user_info


async def process_task_queue():
    """处理任务队列的后台任务"""
    logger.info("启动任务队列处理器")
    
    while True:
        try:
            # 从队列中获取任务
            task = await task_queue.get()
            task_id = task["id"]
            url = task["url"]
            use_speaker_recognition = task.get("use_speaker_recognition", False)
            wechat_webhook = task.get("wechat_webhook", None)
            user_info = task.get("user_info", {})
            
            try:
                # 更新任务状态
                task_results[task_id] = {
                    "status": "processing",
                    "message": "正在处理转录任务"
                }
                
                # 更新数据库中的任务状态
                cache_manager.update_task_status(task_id, "processing")
                
                # 提交任务到线程池，但不等待结果
                future = executor.submit(process_transcription, task_id, url, use_speaker_recognition, wechat_webhook)
                
                # 添加回调函数来处理任务完成
                def task_completed(future_result):
                    try:
                        result = future_result.result()
                        task_results[task_id] = result
                        logger.info(f"任务完成: {task_id}")
                    except Exception as e:
                        logger.exception(f"任务处理失败: {task_id}, URL: {url}, 错误: {str(e)}")
                        
                        # 更新任务状态为失败
                        task_results[task_id] = {
                            "status": "failed",
                            "message": f"转录任务失败: {str(e)}",
                            "error": str(e)
                        }
                        
                        # 发送错误通知
                        task_notifier.notify_task_status(url, "转录失败", str(e))
                
                # 添加回调函数
                future.add_done_callback(task_completed)
                
                logger.info(f"任务已提交到线程池: {task_id}, URL: {url}")
                
            except Exception as e:
                logger.exception(f"提交任务到线程池失败: {task_id}, URL: {url}, 错误: {str(e)}")
                
                # 更新任务状态为失败
                task_results[task_id] = {
                    "status": "failed",
                    "message": f"提交任务失败: {str(e)}",
                    "error": str(e)
                }
            finally:
                # 标记任务完成（从队列角度）
                task_queue.task_done()
        except Exception as e:
            logger.exception(f"任务队列处理器异常: {str(e)}")
            await asyncio.sleep(1)  # 防止过快重试


def process_transcription(task_id, url, use_speaker_recognition=False, wechat_webhook=None):
    """
    处理视频转录
    
    参数:
        task_id: 任务ID
        url: 视频URL
        use_speaker_recognition: 是否使用说话人识别功能
        wechat_webhook: 自定义的企业微信webhook地址
        
    返回:
        dict: 包含转录结果的字典
    """
    try:
        logger.info(f"开始处理转录任务: {task_id}, URL: {url}")
        
        # 创建本任务专用的通知器（如果提供了自定义webhook）
        task_notifier = WechatNotifier(wechat_webhook) if wechat_webhook else wechat_notifier
        
        # 通知任务开始
        task_notifier.notify_task_status(url, "开始处理")
        
        # 创建下载器
        downloader = create_downloader(url)
        if not downloader:
            error_msg = f"不支持的URL类型: {url}"
            logger.error(error_msg)
            task_notifier.notify_task_status(url, "下载失败", error_msg)
            return {
                "status": "failed",
                "message": error_msg
            }
        
        # 检查是否是通用下载器
        is_generic_downloader = downloader.__class__.__name__ == "GenericDownloader"
        
        # ======= 新增：先尝试从URL解析平台和视频ID，然后检查缓存 =======
        platform = None
        video_id = None
        
        # 根据下载器类型识别平台
        downloader_class_name = downloader.__class__.__name__
        if downloader_class_name == "DouyinDownloader":
            platform = "douyin"
            video_id = downloader.extract_video_id(url)
        elif downloader_class_name == "BilibiliDownloader":
            platform = "bilibili"
            video_id = downloader.extract_video_id(url)
        elif downloader_class_name == "XiaohongshuDownloader":
            platform = "xiaohongshu"
            # 小红书链接不再预先解析ID，而是在下载器内部处理
            try:
                video_id = downloader.extract_note_id(url)
            except:
                # 如果提取ID失败，不影响后续流程
                logger.warning(f"预先提取小红书笔记ID失败，将在下载器中处理: {url}")
                video_id = None
        elif downloader_class_name == "YoutubeDownloader":
            platform = "youtube"
            video_id = downloader.extract_video_id(url)
        elif downloader_class_name == "XiaoyuzhouDownloader":
            platform = "xiaoyuzhou"
            video_id = downloader.extract_video_id(url)
        elif downloader_class_name == "GenericDownloader":
            platform = "generic"
            video_id = downloader.extract_video_id(url)
        
        video_title = ""
        author = ""
        description = ""
        is_from_generic = False  # 初始化通用下载器标记
        cache_data = None
        
        # 通用下载器不检查缓存
        if video_id and platform and not is_generic_downloader:
            logger.info(f"从URL中解析出平台: {platform}，视频ID: {video_id}")
            
            # 使用新的缓存系统查找缓存
            cache_data = cache_manager.get_cache(
                platform=platform, 
                media_id=video_id, 
                use_speaker_recognition=use_speaker_recognition
            )
        
        if cache_data:
            logger.info(f"找到已存在的缓存记录，跳过下载和转录步骤")
            
            # 从缓存数据中获取信息
            video_title = cache_data.get("title", "已缓存视频")
            author = cache_data.get("author", "")
            description = cache_data.get("description", "")
            has_speaker_recognition = cache_data.get("use_speaker_recognition", False)
            
            # 处理转录数据
            transcript = ""
            transcription_data = None
            
            if cache_data['transcript_type'] == 'funasr':
                # 处理 FunASR 格式的数据
                transcription_data = cache_data['transcript_data']
                # 使用 FunASRSpeakerClient 格式化转录文本
                funasr_client = FunASRSpeakerClient()
                transcript = funasr_client.format_transcript_with_speakers(transcription_data)
                logger.info(f"使用 FunASR 缓存，包含说话人信息: {len(transcription_data.get('speakers', []))} 个说话人")
            else:
                # 处理 CapsWriter 格式的数据
                transcript = cache_data['transcript_data']
                logger.info("使用 CapsWriter 缓存")
            
            # 检查缓存中是否已有 LLM 结果
            has_llm_calibrated = 'llm_calibrated' in cache_data
            has_llm_summary = 'llm_summary' in cache_data
            
            if has_llm_calibrated and has_llm_summary:
                # 如果已有 LLM 结果，直接使用
                logger.info(f"缓存中已有 LLM 结果，直接使用")
                
                # 通知用户使用缓存的 LLM 结果
                cache_type = "含说话人识别" if has_speaker_recognition else "普通转录"
                task_notifier.notify_task_status(
                    url, 
                    f"使用已有缓存({cache_type}，含LLM结果)", 
                    title=video_title, 
                    author=author, 
                    transcript="使用缓存的校对和总结文本..."
                )
                
                # 直接发送缓存的 LLM 结果
                from utils.wechat import send_long_text_wechat
                
                # 根据校对文本长度判断是否发送校对文本
                calibrated_text = cache_data.get('llm_calibrated', '')
                calibrated_text_length = len(calibrated_text)
                
                # 从配置中获取校对文本最大长度阈值
                wechat_config = config.get('wechat', {})
                calibrated_text_max_length = wechat_config.get('calibrated_text_max_length', 5000)
                
                should_send_calibrated_text = calibrated_text_length <= calibrated_text_max_length
                
                logger.info(f"缓存模式 - 校对文本长度: {calibrated_text_length}, 阈值: {calibrated_text_max_length}, 是否发送校对文本: {should_send_calibrated_text}")
                
                if should_send_calibrated_text:
                    # 校对文本不超过阈值：发送校对文本和总结文本
                    logger.info("缓存模式 - 校对文本长度合适，发送校对文本和总结文本")
                    # 发送校对文本（确保使用限流）
                    send_long_text_wechat(
                        title=video_title,
                        url=url,
                        text=cache_data['llm_calibrated'],
                        is_summary=False,
                        has_speaker_recognition=has_speaker_recognition,
                        webhook=wechat_webhook,
                        use_rate_limit=True
                    )
                    
                    # 确保校对文本完全加入队列后再发送总结文本
                    import time
                    logger.info(f"[缓存模式] 校对文本发送完成，延迟100ms后发送总结文本")
                    time.sleep(0.1)  # 100ms延迟，确保所有校对文本分段都已加入队列
                    
                    # 发送总结文本（确保使用限流）
                    send_long_text_wechat(
                        title=video_title,
                        url=url,
                        text=cache_data['llm_summary'],
                        is_summary=True,
                        has_speaker_recognition=has_speaker_recognition,
                        webhook=wechat_webhook,
                        use_rate_limit=True
                    )
                    
                    # 确保总结文本完全加入队列后再发送完成通知
                    logger.info(f"[缓存模式] 总结文本发送完成，延迟100ms后发送完成通知")
                    time.sleep(0.1)  # 100ms延迟，确保总结文本已加入队列
                else:
                    # 校对文本超过阈值：只发送总结文本（校对文本太长了）
                    logger.info("缓存模式 - 校对文本过长，跳过校对文本发送，只发送总结文本")
                    send_long_text_wechat(
                        title=video_title,
                        url=url,
                        text=cache_data['llm_summary'],
                        is_summary=True,
                        has_speaker_recognition=has_speaker_recognition,
                        webhook=wechat_webhook,
                        use_rate_limit=True
                    )
                    
                    # 确保总结文本完全加入队列后再发送完成通知
                    import time
                    logger.info(f"[缓存模式] 总结文本发送完成（校对文本过长），延迟100ms后发送完成通知")
                    time.sleep(0.1)  # 100ms延迟，确保总结文本已加入队列
                
                # 发送任务完成通知，包含查看链接  
                task_info = cache_manager.get_task_by_id(task_id)
                if task_info and task_info.get('view_token'):
                    from utils.wechat import send_view_link_wechat
                    from utils.markdown_renderer import get_base_url
                    
                    base_url = get_base_url()
                    view_url = f"{base_url}/view/{task_info['view_token']}"
                    
                    # 使用限流系统发送完成通知，确保顺序正确
                    completion_message = f"✅ 【任务完成】{video_title}\n\n转录和AI处理已全部完成！\n\n🔗 查看完整结果：{view_url}"
                    logger.info(f"[缓存模式] 准备发送任务完成通知: {video_title}")
                    task_notifier = WechatNotifier(wechat_webhook, use_rate_limit=True)
                    task_notifier.send_text(completion_message)
                    logger.info(f"[缓存模式] 任务完成通知已加入限流队列: {task_id}")
                
                logger.info(f"已发送缓存的 LLM 结果: {video_title}")
                
                # 更新任务状态为成功
                cache_manager.update_task_status(
                    task_id, 
                    "success", 
                    platform=cache_data.get("platform"),
                    media_id=cache_data.get("media_id"),
                    title=video_title,
                    author=author,
                    cache_id=cache_data.get("cache_id")
                )
            else:
                # 如果没有 LLM 结果，才加入队列处理
                logger.info(f"缓存中没有 LLM 结果，需要重新处理")
                
                # 通知用户我们使用的是缓存的转录
                cache_type = "含说话人识别" if has_speaker_recognition else "普通转录"
                task_notifier.notify_task_status(
                    url, 
                    f"使用已有缓存({cache_type})", 
                    title=video_title, 
                    author=author, 
                    transcript="正在处理已存在的转录文本..."
                )
                
                # 将LLM处理任务加入队列
                try:
                    llm_task = {
                        "task_id": task_id,
                        "url": url,
                        "platform": cache_data.get("platform"),
                        "media_id": cache_data.get("media_id"),
                        "video_title": video_title,
                        "author": author,
                        "description": description,  # 现在从元数据缓存中获取
                        "transcript": transcript,
                        "use_speaker_recognition": has_speaker_recognition,
                        "transcription_data": transcription_data if has_speaker_recognition else None,
                        "is_generic": is_generic_downloader or is_from_generic,  # 传递通用下载器标记
                        "wechat_webhook": wechat_webhook  # 传递自定义webhook
                    }
                    
                    logger.info(f"将LLM任务加入队列: {task_id}, 标题: {video_title}, 说话人识别: {has_speaker_recognition}")
                    
                    # 将LLM任务放入线程安全队列中
                    llm_task_queue.put(llm_task)
                    
                except Exception as e:
                    logger.exception(f"将LLM任务加入队列失败: {str(e)}")
                    task_notifier.send_text(f"【LLM任务加入队列失败】{str(e)}")
            
            return {
                "status": "success",
                "message": "使用已有缓存成功",
                "data": {
                    "video_title": video_title,
                    "author": author,
                    "transcript": transcript,
                    "cached": True,
                    "speaker_recognition": has_speaker_recognition
                }
            }
        # ======= 缓存检查逻辑结束 =======
        
        # 如果没有找到缓存，则获取完整的视频信息
        logger.info(f"未找到缓存文件，获取视频信息: {url}")
        
        # 小红书链接使用原始URL直接获取视频信息
        video_info = downloader.get_video_info(url)
        
        # 提取视频标题、作者和描述
        video_title = video_info.get("video_title", "")
        author = video_info.get("author", "")
        description = video_info.get("description", "")
        
        # 检查是否来自通用下载器
        is_from_generic = video_info.get("is_generic", False)
        
        # 根据 use_speaker_recognition 参数决定处理优先级
        subtitle = None
        
        if use_speaker_recognition:
            # 如果需要说话人识别，强制跳过平台字幕，直接进行下载转录
            logger.info(f"需要说话人识别，跳过平台字幕获取，强制下载转录: {url}")
        else:
            # 只有在不需要说话人识别时，才尝试获取平台字幕
            if downloader.__class__.__name__ == "YoutubeDownloader":
                logger.info(f"不需要说话人识别，尝试获取YouTube平台字幕: {url}")
                subtitle = downloader.get_subtitle(url)
            
        if subtitle:
            # 如果有字幕，直接使用
            logger.info(f"使用平台提供的字幕: {url}")
            
            # 通知获取平台字幕成功
            task_notifier.notify_task_status(
                url, 
                "平台字幕获取成功", 
                title=video_title, 
                author=author
            )
            
            # 使用新的缓存系统保存平台字幕
            cache_result = cache_manager.save_cache(
                platform=video_info.get('platform'),
                url=url,
                media_id=video_info.get('video_id'),
                use_speaker_recognition=False,  # 平台字幕没有说话人识别
                transcript_data=subtitle,
                transcript_type='capswriter',  # 平台字幕按文本格式保存
                title=video_title,
                author=author,
                description=description
            )
            
            if not cache_result:
                logger.error("保存平台字幕到缓存失败")
            
            # 通知转录完成，包含标题、作者和转录文本
            # wechat_notifier.notify_task_status(
            #     url, 
            #     "转录完成", 
            #     title=video_title, 
            #     author=author, 
            #     transcript=subtitle
            # )
            
            # ======= 新增：将LLM处理任务加入队列 =======
            try:
                llm_task = {
                    "task_id": task_id,
                    "url": url,
                    "platform": video_info.get('platform'),
                    "media_id": video_info.get('video_id'),
                    "video_title": video_title,
                    "author": author,
                    "description": description,
                    "transcript": subtitle,
                    "use_speaker_recognition": False,  # 平台字幕没有说话人信息
                    "is_generic": is_generic_downloader or is_from_generic,  # 传递通用下载器标记
                    "wechat_webhook": wechat_webhook  # 传递自定义webhook
                }
                
                logger.info(f"将LLM任务加入队列（平台字幕）: {task_id}, 标题: {video_title}")
                
                # 将LLM任务放入线程安全队列中
                llm_task_queue.put(llm_task)
                
            except Exception as e:
                logger.exception(f"将LLM任务加入队列失败（平台字幕）: {str(e)}")
                task_notifier.send_text(f"【LLM任务加入队列失败】{str(e)}")
            # ======= END =======
            
            result = {
                "status": "success",
                "message": "使用平台字幕成功",
                "data": {
                    "video_title": video_title,
                    "author": author,
                    "transcript": subtitle
                }
            }
        else:
            # 没有字幕，需要下载音视频并转录
            logger.info(f"下载视频进行转录: {url}")
            task_notifier.notify_task_status(url, "正在下载视频", title=video_title, author=author)
            
            # 检查是否已通过BBDown下载
            local_file = None
            if video_info.get("downloaded", False) and video_info.get("local_file"):
                # 使用BBDown已下载的文件
                local_file = video_info.get("local_file")
                logger.info(f"使用BBDown已下载的文件: {local_file}")
            else:
                # 常规下载流程
                download_url = video_info.get("download_url")
                filename = video_info.get("filename")
                
                if not download_url or not filename:
                    error_msg = f"无法获取下载信息: {url}"
                    logger.error(error_msg)
                    task_notifier.notify_task_status(url, "下载失败", error_msg, title=video_title, author=author)
                    return {
                        "status": "failed",
                        "message": error_msg
                    }
                
                # 下载文件
                local_file = downloader.download_file(download_url, filename)
                if not local_file:
                    error_msg = f"下载文件失败: {url}"
                    logger.error(error_msg)
                    task_notifier.notify_task_status(url, "下载失败", error_msg, title=video_title, author=author)
                    return {
                        "status": "failed",
                        "message": error_msg
                    }
            
            try:
                # 开始转录
                logger.info(f"开始转录音视频: {local_file}")
                task_notifier.notify_task_status(url, "正在转录音视频", title=video_title, author=author)
                
                # 获取平台和媒体ID
                platform = video_info.get('platform')
                media_id = video_info.get('video_id')
                
                # 根据是否需要说话人识别选择转录器
                if use_speaker_recognition:
                    # 使用 FunASR 说话人识别服务器
                    logger.info("使用 FunASR 说话人识别服务器进行转录")
                    funasr_client = FunASRSpeakerClient()
                    funasr_result = funasr_client.transcribe_sync(local_file)
                    
                    # 获取格式化的转录文本
                    transcript = funasr_result["formatted_text"]
                    transcription_data = funasr_result["transcription_result"]
                    
                    # 使用新缓存系统保存
                    cache_result = cache_manager.save_cache(
                        platform=platform,
                        url=url,
                        media_id=media_id,
                        use_speaker_recognition=True,
                        transcript_data=transcription_data,
                        transcript_type='funasr',
                        title=video_title,
                        author=author,
                        description=description
                    )
                    
                    if not cache_result:
                        logger.error("保存FunASR转录结果到缓存失败")
                    
                    # 构造与普通转录器兼容的结果
                    transcription_result = {
                        "transcript": transcript,
                        "speaker_recognition": True,
                        "transcription_data": transcription_data
                    }
                else:
                    # 使用普通 CapsWriter 转录器
                    transcriber = Transcriber()
                    # 使用时间戳作为临时输出基础名
                    temp_output_base = datetime.datetime.now().strftime("%y%m%d-%H%M%S")
                    transcription_result = transcriber.transcribe(local_file, temp_output_base)
                    transcript = transcription_result.get("transcript", "")
                    
                    # 使用新缓存系统保存
                    cache_result = cache_manager.save_cache(
                        platform=platform,
                        url=url,
                        media_id=media_id,
                        use_speaker_recognition=False,
                        transcript_data=transcript,
                        transcript_type='capswriter',
                        title=video_title,
                        author=author,
                        description=description
                    )
                    
                    if not cache_result:
                        logger.error("保存CapsWriter转录结果到缓存失败")
                
                # 获取转录文本
                transcript = transcription_result.get("transcript", "")
                
                # 通知转录完成，包含转录文本预览
                task_notifier.notify_task_status(
                    url, 
                    "转录完成", 
                    title=video_title, 
                    author=author, 
                    transcript=transcript
                )
                
                # ======= 新增：将LLM处理任务加入队列 =======
                try:
                    llm_task = {
                        "task_id": task_id,
                        "url": url,
                        "platform": platform,
                        "media_id": media_id,
                        "video_title": video_title,
                        "author": author,
                        "description": description,
                        "transcript": transcript,
                        "use_speaker_recognition": use_speaker_recognition,
                        "transcription_data": transcription_result.get("transcription_data") if use_speaker_recognition else None,
                        "is_generic": is_generic_downloader or is_from_generic,  # 传递通用下载器标记
                        "wechat_webhook": wechat_webhook  # 传递自定义webhook
                    }
                    
                    logger.info(f"将LLM任务加入队列（常规转录）: {task_id}, 标题: {video_title}")
                    
                    # 将LLM任务放入线程安全队列中
                    llm_task_queue.put(llm_task)
                    
                except Exception as e:
                    logger.exception(f"将LLM任务加入队列失败（常规转录）: {str(e)}")
                    task_notifier.send_text(f"【LLM任务加入队列失败】{str(e)}")
                # ======= END =======
                
                # 返回结果
                result = {
                    "status": "success",
                    "message": "转录成功",
                    "data": {
                        "video_title": video_title,
                        "author": author,
                        "transcript": transcript,
                        "speaker_recognition": use_speaker_recognition
                    }
                }
            finally:
                # 清理下载的文件
                logger.info(f"清理下载的文件: {local_file}")
                downloader.clean_up(local_file)
        
        # 更新任务状态为成功
        cache_manager.update_task_status(
            task_id, 
            "success", 
            platform=video_info.get('platform'),
            media_id=video_info.get('video_id'), 
            title=video_title,
            author=author
        )
        
        return result
    except Exception as e:
        logger.exception(f"转录处理异常: {str(e)}")
        task_notifier.notify_task_status(url, "转录异常", str(e))
        
        # 更新任务状态为失败
        cache_manager.update_task_status(task_id, "failed")
        
        return {
            "status": "failed",
            "message": f"转录任务异常: {str(e)}",
            "error": str(e)
        }


def process_llm_queue():
    """
    处理LLM队列的后台任务（在单独线程中运行）
    确保同一视频的校对和总结文本按顺序连续发送
    """
    logger.info("启动LLM队列处理器")
    
    while True:
        try:
            # 从LLM队列中获取任务（阻塞等待）
            llm_task = llm_task_queue.get()
            
            # 获取锁，确保同一时间只处理一个视频的LLM任务
            with llm_processing_lock:
                task_id = llm_task["task_id"]
                url = llm_task["url"]
                platform = llm_task.get("platform")
                media_id = llm_task.get("media_id")
                video_title = llm_task["video_title"]
                author = llm_task["author"]
                description = llm_task.get("description", "")
                transcript = llm_task["transcript"]
                use_speaker_recognition = llm_task.get("use_speaker_recognition", False)
                transcription_data = llm_task.get("transcription_data")
                wechat_webhook = llm_task.get("wechat_webhook")
                
                # 创建本任务专用的通知器（如果提供了自定义webhook）
                task_notifier = WechatNotifier(wechat_webhook) if wechat_webhook else wechat_notifier
                
                logger.info(f"开始处理LLM任务: {task_id}, 标题: {video_title}")
                
                try:
                    # 使用增强LLM处理器进行校对和总结（支持自动分段）
                    from utils.wechat import send_long_text_wechat
                    from utils.llm import call_llm_api
                    
                    # 如果是通用下载器且没有标题，先生成标题
                    if video_title == "":
                        # 检查是否有通用下载器标记
                        is_generic = llm_task.get("is_generic", False)
                        if is_generic:
                            logger.info("通用下载器文件没有标题，使用LLM生成")
                            
                            # 生成标题的prompt
                            title_prompt = (
                                "请根据以下音视频转录文本，生成一个简洁的标题（不超过20个字）。\n"
                                "只返回标题文本，不要有任何其他说明或标点符号。\n"
                                "如果无法从内容中提取有意义的标题，请返回'自定义文件总结'。\n\n"
                                "转录文本：\n" + transcript[:1000]  # 只使用前1000字符生成标题
                            )
                            
                            try:
                                config_llm = config.get("llm", {})
                                api_key = config_llm.get("api_key")
                                base_url = config_llm.get("base_url")
                                summary_model = config_llm.get("summary_model")
                                max_retries = config_llm.get("max_retries", 2)
                                retry_delay = config_llm.get("retry_delay", 5)
                                generated_title = call_llm_api(summary_model, title_prompt, api_key, base_url, max_retries, retry_delay)
                                
                                # 清理生成的标题
                                generated_title = generated_title.strip().strip('"').strip("'").strip("。").strip("，")
                                if generated_title and len(generated_title) <= 30:
                                    video_title = generated_title
                                    logger.info(f"LLM生成的标题: {video_title}")
                                else:
                                    video_title = "自定义文件总结"
                                    logger.warning(f"LLM生成的标题不合规，使用默认标题")
                            except Exception as e:
                                logger.error(f"LLM生成标题失败: {str(e)}")
                                video_title = "自定义文件总结"
                    
                    # 更新任务中的标题
                    llm_task["video_title"] = video_title
                    
                    # 使用增强LLM处理器处理任务（自动判断是否需要分段）
                    logger.info(f"开始使用增强LLM处理器处理任务: {task_id}")
                    result_dict = enhanced_llm_processor.process_llm_task(llm_task)
                    
                    logger.info(f"LLM处理完成，开始保存结果和发送微信通知: {task_id}")
                    
                    # 保存校对文本到缓存
                    if platform and media_id:
                        cache_manager.save_llm_result(
                            platform=platform,
                            media_id=media_id,
                            use_speaker_recognition=use_speaker_recognition,
                            llm_type="calibrated",
                            content=result_dict['校对文本']
                        )
                        
                        # 保存总结文本到缓存
                        cache_manager.save_llm_result(
                            platform=platform,
                            media_id=media_id,
                            use_speaker_recognition=use_speaker_recognition,
                            llm_type="summary",
                            content=result_dict['内容总结']
                        )
                        logger.info(f"LLM结果已保存到缓存: {platform}/{media_id}")
                    
                    # 根据校对文本长度判断是否发送校对文本
                    calibrated_text = result_dict.get('校对文本', '')
                    calibrated_text_length = len(calibrated_text)
                    
                    # 从配置中获取校对文本最大长度阈值
                    wechat_config = config.get('wechat', {})
                    calibrated_text_max_length = wechat_config.get('calibrated_text_max_length', 5000)
                    
                    should_send_calibrated_text = calibrated_text_length <= calibrated_text_max_length
                    
                    logger.info(f"校对文本长度: {calibrated_text_length}, 阈值: {calibrated_text_max_length}, 是否发送校对文本: {should_send_calibrated_text}")
                    
                    if should_send_calibrated_text:
                        # 校对文本不超过阈值：发送校对文本和总结文本
                        logger.info("校对文本长度合适，发送校对文本和总结文本")
                        # 校对文本分段发送（确保使用限流）
                        send_long_text_wechat(
                            title=video_title,
                            url=url,
                            text=result_dict['校对文本'],
                            is_summary=False,
                            has_speaker_recognition=use_speaker_recognition,
                            webhook=wechat_webhook,
                            use_rate_limit=True
                        )
                        
                        # 确保校对文本完全加入队列后再发送总结文本
                        import time
                        time.sleep(0.1)  # 100ms延迟，确保所有校对文本分段都已加入队列
                        
                        # 总结文本直接发送（确保使用限流）
                        send_long_text_wechat(
                            title=video_title,
                            url=url,
                            text=result_dict['内容总结'],
                            is_summary=True,
                            has_speaker_recognition=use_speaker_recognition,
                            webhook=wechat_webhook,
                            use_rate_limit=True
                        )
                        
                        # 确保总结文本完全加入队列后再发送完成通知
                        time.sleep(0.1)  # 100ms延迟，确保总结文本已加入队列
                    else:
                        # 校对文本超过阈值：只发送总结文本（校对文本太长了）
                        logger.info("校对文本过长，跳过校对文本发送，只发送总结文本")
                        send_long_text_wechat(
                            title=video_title,
                            url=url,
                            text=result_dict['内容总结'],
                            is_summary=True,
                            has_speaker_recognition=use_speaker_recognition,
                            webhook=wechat_webhook,
                            use_rate_limit=True
                        )
                        
                        # 确保总结文本完全加入队列后再发送完成通知
                        import time
                        time.sleep(0.1)  # 100ms延迟，确保总结文本已加入队列
                    
                    # 发送任务完成通知，包含查看链接
                    task_info = cache_manager.get_task_by_id(task_id)
                    if task_info and task_info.get('view_token'):
                        from utils.markdown_renderer import get_base_url
                        
                        base_url = get_base_url()
                        view_url = f"{base_url}/view/{task_info['view_token']}"
                        
                        # 使用限流系统发送完成通知，确保顺序正确
                        completion_message = f"✅ 【任务完成】{video_title}\n\n转录和AI处理已全部完成！\n\n🔗 查看完整结果：{view_url}"
                        task_notifier = WechatNotifier(wechat_webhook, use_rate_limit=True)
                        task_notifier.send_text(completion_message)
                        logger.info(f"任务完成通知已加入限流队列: {task_id}")
                    
                    logger.info(f"LLM任务处理完成: {task_id}, 标题: {video_title}")
                    
                except Exception as e:
                    logger.exception(f"LLM任务处理异常: {task_id}, 错误: {str(e)}")
                    task_notifier.send_text(f"【LLM API调用异常】{str(e)}")
                finally:
                    # 标记LLM任务完成
                    llm_task_queue.task_done()
        except Exception as e:
            logger.exception(f"LLM队列处理器异常: {str(e)}")
            import time
            time.sleep(1)  # 防止过快重试


@app.on_event("startup")
async def startup_event():
    """服务启动时执行"""
    # 启动任务队列处理器
    asyncio.create_task(process_task_queue())
    
    # 启动LLM队列处理器（在单独线程中运行）
    llm_thread = threading.Thread(target=process_llm_queue, daemon=True)
    llm_thread.start()
    
    logger.info("API服务已启动，转录队列和LLM队列处理器已启动")


@app.get("/add_task_by_web", response_class=HTMLResponse)
async def add_task_by_web():
    """
    Web添加任务页面路由
    
    Returns:
        FileResponse: 返回index.html文件
    """
    try:
        index_file = os.path.join(static_dir, "index.html")
        if os.path.exists(index_file):
            return FileResponse(index_file)
        else:
            logger.error(f"Web任务添加页面文件不存在: {index_file}")
            raise HTTPException(status_code=404, detail="Web任务添加页面文件未找到")
    except Exception as e:
        logger.exception(f"访问Web任务添加页面异常: {str(e)}")
        raise HTTPException(status_code=500, detail=f"访问Web任务添加页面失败: {str(e)}")


@app.post("/api/transcribe", response_model=TranscribeResponse)
async def transcribe_video(
    request_body: TranscribeRequest, 
    background_tasks: BackgroundTasks,
    request: Request,
    user_info: dict = Depends(verify_token)
):
    """
    转录视频接口
    
    请求参数:
        url: 视频URL
        
    返回:
        TranscribeResponse: 包含转录结果的响应
    """
    url = request_body.url
    if not url:
        logger.warning("请求未提供视频URL")
        raise HTTPException(status_code=400, detail="视频URL不能为空")
    
    # 记录API调用开始时间
    start_time = datetime.datetime.now()
    
    # 获取用户信息
    user_id = user_info.get("user_id")
    api_key = user_info.get("api_key")
    
    # 记录API调用审计日志（开始）
    audit_logger.log_api_call(
        api_key=api_key,
        user_id=user_id,
        endpoint="/api/transcribe",
        video_url=url,
        user_agent=request.headers.get("User-Agent"),
        remote_ip=request.client.host if request.client else None
    )
    
    try:
        # 创建任务并生成唯一ID和view_token
        task_info = cache_manager.create_task(url, request_body.use_speaker_recognition)
        task_id = task_info["task_id"]
        view_token = task_info["view_token"]
        
        # 初始化任务状态（兼容现有代码）
        task_results[task_id] = {
            "status": "queued",
            "message": "任务已加入队列",
            "view_token": view_token  # 添加view_token供查看使用
        }
        
        # 添加任务到队列
        try:
            # 确定要使用的企微webhook地址（优先级：请求参数 > 用户配置 > 全局配置）
            effective_webhook = (
                request_body.wechat_webhook or 
                user_info.get("wechat_webhook") or 
                config.get("wechat", {}).get("webhook")
            )
            
            task = {
                "id": task_id, 
                "url": url, 
                "use_speaker_recognition": request_body.use_speaker_recognition,
                "wechat_webhook": effective_webhook,
                "user_info": user_info  # 添加用户信息到任务中
            }
            await task_queue.put(task)
            logger.info(f"任务已加入队列: {task_id}, URL: {url}")
            
            # 立即发送企微通知，包含查看链接
            try:
                from utils.wechat import send_view_link_wechat
                
                # 尝试从URL获取简单的标题信息（不进行复杂解析）
                title = "转录任务已创建"
                if "youtube.com" in url or "youtu.be" in url:
                    title = "YouTube视频转录"
                elif "bilibili.com" in url:
                    title = "Bilibili视频转录"
                elif "xiaoyuzhoufm.com" in url:
                    title = "小宇宙播客转录"
                elif "xiaohongshu.com" in url:
                    title = "小红书内容转录"
                elif "douyin.com" in url:
                    title = "抖音视频转录"
                
                # 发送初始通知
                send_view_link_wechat(
                    title=f"🎬 {title}",
                    view_token=view_token,
                    webhook=effective_webhook
                )
                logger.info(f"已发送任务创建通知: {task_id}")
                
            except Exception as e:
                logger.exception(f"发送任务创建通知失败: {task_id}, 错误: {str(e)}")
                # 通知失败不应该影响任务创建
                
        except asyncio.QueueFull:
            logger.warning(f"任务队列已满，拒绝任务: {url}")
            raise HTTPException(status_code=503, detail="任务队列已满，请稍后重试")
        
        # 计算处理时间并记录成功的审计日志
        processing_time_ms = int((datetime.datetime.now() - start_time).total_seconds() * 1000)
        audit_logger.log_api_call(
            api_key=api_key,
            user_id=user_id,
            endpoint="/api/transcribe",
            video_url=url,
            processing_time_ms=processing_time_ms,
            status_code=202,
            task_id=task_id,
            user_agent=request.headers.get("User-Agent"),
            remote_ip=request.client.host if request.client else None
        )
        
        # 返回任务ID和view_token
        return TranscribeResponse(
            code=202,
            message="任务已提交",
            data={
                "task_id": task_id,
                "view_token": view_token
            }
        )
    except HTTPException as he:
        # 记录HTTP异常的审计日志
        processing_time_ms = int((datetime.datetime.now() - start_time).total_seconds() * 1000)
        audit_logger.log_api_call(
            api_key=api_key,
            user_id=user_id,
            endpoint="/api/transcribe",
            video_url=url,
            processing_time_ms=processing_time_ms,
            status_code=he.status_code,
            user_agent=request.headers.get("User-Agent"),
            remote_ip=request.client.host if request.client else None
        )
        raise
    except Exception as e:
        # 记录一般异常的审计日志
        processing_time_ms = int((datetime.datetime.now() - start_time).total_seconds() * 1000)
        audit_logger.log_api_call(
            api_key=api_key,
            user_id=user_id,
            endpoint="/api/transcribe",
            video_url=url,
            processing_time_ms=processing_time_ms,
            status_code=500,
            user_agent=request.headers.get("User-Agent"),
            remote_ip=request.client.host if request.client else None
        )
        logger.exception(f"提交转录任务失败: {str(e)}")
        raise HTTPException(status_code=500, detail=f"提交转录任务失败: {str(e)}")


@app.get("/api/task/{task_id}", response_model=TranscribeResponse)
async def get_task_status(
    task_id: str,
    request: Request,
    user_info: dict = Depends(verify_token)
):
    """
    获取任务状态接口
    
    请求参数:
        task_id: 任务ID
        
    返回:
        TranscribeResponse: 包含任务状态的响应
    """
    # 记录API调用审计日志
    start_time = datetime.datetime.now()
    user_id = user_info.get("user_id")
    api_key = user_info.get("api_key")
    
    try:
        if task_id not in task_results:
            # 记录任务不存在的审计日志
            processing_time_ms = int((datetime.datetime.now() - start_time).total_seconds() * 1000)
            audit_logger.log_api_call(
                api_key=api_key,
                user_id=user_id,
                endpoint=f"/api/task/{task_id}",
                processing_time_ms=processing_time_ms,
                status_code=404,
                task_id=task_id,
                user_agent=request.headers.get("User-Agent"),
                remote_ip=request.client.host if request.client else None
            )
            logger.warning(f"任务不存在: {task_id}")
            raise HTTPException(status_code=404, detail=f"任务不存在: {task_id}")
        
        task_result = task_results[task_id]
        
        # 根据任务状态设置响应码
        code = 200
        if task_result.get("status") == "queued" or task_result.get("status") == "processing":
            code = 202  # 处理中
        elif task_result.get("status") == "failed":
            code = 500  # 失败
        
        # 记录成功的审计日志
        processing_time_ms = int((datetime.datetime.now() - start_time).total_seconds() * 1000)
        audit_logger.log_api_call(
            api_key=api_key,
            user_id=user_id,
            endpoint=f"/api/task/{task_id}",
            processing_time_ms=processing_time_ms,
            status_code=code,
            task_id=task_id,
            user_agent=request.headers.get("User-Agent"),
            remote_ip=request.client.host if request.client else None
        )
        
        return TranscribeResponse(
            code=code,
            message=task_result.get("message", "获取任务状态成功"),
            data=task_result.get("data")
        )
    except HTTPException:
        raise
    except Exception as e:
        # 记录异常的审计日志
        processing_time_ms = int((datetime.datetime.now() - start_time).total_seconds() * 1000)
        audit_logger.log_api_call(
            api_key=api_key,
            user_id=user_id,
            endpoint=f"/api/task/{task_id}",
            processing_time_ms=processing_time_ms,
            status_code=500,
            task_id=task_id,
            user_agent=request.headers.get("User-Agent"),
            remote_ip=request.client.host if request.client else None
        )
        logger.exception(f"获取任务状态异常: {str(e)}")
        raise HTTPException(status_code=500, detail=f"获取任务状态失败: {str(e)}")


@app.get("/api/webhook-stats")
async def get_webhook_stats(user_info: dict = Depends(verify_token)):
    """
    获取webhook限流器统计信息
    
    返回:
        dict: 限流器统计数据
    """
    try:
        stats = get_rate_limiter_stats()
        return TranscribeResponse(
            code=200,
            message="获取统计信息成功",
            data=stats
        )
    except Exception as e:
        logger.exception(f"获取webhook统计信息异常: {str(e)}")
        raise HTTPException(status_code=500, detail=f"获取统计信息失败: {str(e)}")


@app.get("/api/webhook-status")
async def get_webhook_status_info(webhook_url: str, user_info: dict = Depends(verify_token)):
    """
    获取指定webhook的状态信息
    
    请求参数:
        webhook_url: webhook地址（URL参数）
        
    返回:
        dict: webhook状态信息
    """
    try:
        if not webhook_url:
            raise HTTPException(status_code=400, detail="webhook地址不能为空")
            
        status = get_webhook_status(webhook_url)
        return TranscribeResponse(
            code=200,
            message="获取webhook状态成功",
            data=status
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"获取webhook状态异常: {str(e)}")
        raise HTTPException(status_code=500, detail=f"获取webhook状态失败: {str(e)}")


@app.get("/view/{view_token}", response_class=HTMLResponse)
async def view_transcript(view_token: str, request: Request):
    """
    查看转录页面（无需认证）
    
    Args:
        view_token: 查看token
        request: FastAPI请求对象
        
    Returns:
        HTMLResponse: 渲染后的HTML页面
    """
    try:
        # 获取页面数据
        view_data = cache_manager.get_view_data_by_token(view_token)
        if not view_data:
            return templates.TemplateResponse(
                "error.html",
                {"request": request, "message": "页面不存在", "title": "404 - 页面未找到"}
            )
        
        # 格式化创建时间（所有状态页面都需要）
        if view_data.get('created_at'):
            view_data['created_at_display'] = format_datetime_for_display(view_data['created_at'])
        
        # 根据状态选择模板
        if view_data['status'] == 'processing':
            return templates.TemplateResponse(
                "processing.html",
                {"request": request, **view_data}
            )
        elif view_data['status'] == 'failed':
            return templates.TemplateResponse(
                "error.html",
                {"request": request, "message": view_data.get('message', '转录失败'), **view_data}
            )
        elif view_data['status'] == 'file_cleaned':
            return templates.TemplateResponse(
                "cleaned.html",
                {"request": request, **view_data}
            )
        elif view_data['status'] == 'success':
            # 服务端渲染内容
            view_data['summary_html'] = render_markdown_to_html(view_data.get('summary', ''))
            
            # 使用智能对话渲染器处理转录文本
            # 优先使用缓存分析的智能渲染，降级到文本检测渲染
            cache_dir = view_data.get('cache_dir')
            fallback_text = view_data.get('transcript', '')
            
            if cache_dir and os.path.exists(cache_dir):
                try:
                    # 使用智能渲染（基于缓存分析）
                    view_data['transcript_html'] = render_transcript_content_smart(cache_dir, fallback_text)
                    
                    # 渲染校对文本（如果存在）
                    calibrated_html = render_calibrated_content_smart(cache_dir)
                    if calibrated_html:
                        view_data['calibrated_html'] = calibrated_html
                        logger.debug(f"校对文本渲染成功: {view_token}")
                    
                    logger.debug(f"使用智能渲染成功: {view_token}")
                    
                    # 检查是否需要后台升级缓存
                    logger.info(f"检查缓存升级需求: {cache_dir}")
                    try:
                        _trigger_cache_upgrade_if_needed(cache_dir, view_data)
                    except Exception as upgrade_e:
                        logger.error(f"缓存升级检查异常: {upgrade_e}", exc_info=True)
                    
                except Exception as e:
                    logger.warning(f"智能渲染失败，降级到文本检测渲染: {e}")
                    # 降级到基础文本检测渲染
                    view_data['transcript_html'] = render_transcript_content(fallback_text)
            else:
                # 没有缓存信息，使用基础文本检测渲染
                view_data['transcript_html'] = render_transcript_content(fallback_text)
            
            return templates.TemplateResponse(
                "transcript.html",
                {"request": request, **view_data}
            )
        else:
            return templates.TemplateResponse(
                "error.html",
                {"request": request, "message": "未知状态", **view_data}
            )
        
    except Exception as e:
        logger.exception(f"查看页面异常: {view_token}, 错误: {str(e)}")
        return templates.TemplateResponse(
            "error.html",
            {"request": request, "message": "服务异常", "title": "服务异常"}
        )

def _trigger_cache_upgrade_if_needed(cache_dir: str, view_data: dict):
    """
    检查并触发缓存升级（如果需要的话）
    
    Args:
        cache_dir: 缓存目录路径
        view_data: 视图数据
    """
    try:
        logger.info(f"进入缓存升级检查: {cache_dir}")
        from ..utils.cache_analyzer import should_upgrade_cache
        from ..utils.llm_enhanced import EnhancedLLMProcessor
        import threading
        import json
        
        # 判断是否需要升级
        should_upgrade = should_upgrade_cache(cache_dir)
        logger.info(f"缓存升级检查结果: {cache_dir} -> {should_upgrade}")
        
        if not should_upgrade:
            logger.info(f"缓存无需升级: {cache_dir}")
            return
        
        logger.info(f"检测到高价值缓存，触发后台升级: {cache_dir}")
        
        # 在后台线程中进行升级，避免阻塞用户响应
        def background_upgrade():
            try:
                # 读取FunASR数据
                funasr_file = os.path.join(cache_dir, 'transcript_funasr.json')
                if not os.path.exists(funasr_file):
                    return
                
                with open(funasr_file, 'r', encoding='utf-8') as f:
                    funasr_data = json.load(f)
                
                # 构建视频元数据
                video_metadata = {
                    'video_title': view_data.get('title', '未知标题'),
                    'author': view_data.get('author', '未知作者'), 
                    'description': view_data.get('description', '')
                }
                
                # 使用增强LLM处理器进行升级
                config = load_config()
                llm_processor = EnhancedLLMProcessor(config)
                
                # 检查是否应该使用结构化处理
                if llm_processor.should_use_structured_processing(cache_dir):
                    logger.info(f"开始结构化升级: {cache_dir}")
                    result = llm_processor.process_llm_task_with_structure(
                        cache_dir, funasr_data, video_metadata
                    )
                    logger.info(f"缓存升级完成: {cache_dir}")
                else:
                    logger.debug(f"缓存无需升级: {cache_dir}")
                    
            except Exception as e:
                logger.error(f"后台缓存升级失败: {cache_dir}, {e}")
        
        # 在后台线程中执行升级
        upgrade_thread = threading.Thread(target=background_upgrade, daemon=True)
        upgrade_thread.start()
        
    except Exception as e:
        logger.error(f"触发缓存升级失败: {e}")


@app.get("/api/audit/stats")
async def get_audit_stats(
    days: int = 30,
    user_info: dict = Depends(verify_token)
):
    """
    获取API调用统计信息
    
    请求参数:
        days: 统计天数，默认30天
        
    返回:
        dict: 统计信息
    """
    try:
        user_id = user_info.get("user_id")
        
        # 获取用户自己的统计信息
        user_stats = audit_logger.get_user_stats(user_id, days)
        
        return TranscribeResponse(
            code=200,
            message="获取统计信息成功",
            data={
                "user_stats": user_stats,
                "is_multi_user_mode": user_manager.is_multi_user_mode(),
                "total_users": user_manager.get_user_count()
            }
        )
    except Exception as e:
        logger.exception(f"获取审计统计异常: {str(e)}")
        raise HTTPException(status_code=500, detail=f"获取统计信息失败: {str(e)}")


@app.get("/api/audit/calls")
async def get_audit_calls(
    limit: int = 100,
    user_info: dict = Depends(verify_token)
):
    """
    获取最近的API调用记录
    
    请求参数:
        limit: 返回记录数量，默认100
        
    返回:
        list: API调用记录
    """
    try:
        user_id = user_info.get("user_id")
        
        # 用户只能查看自己的调用记录
        recent_calls = audit_logger.get_recent_calls(user_id, limit)
        
        return TranscribeResponse(
            code=200,
            message="获取调用记录成功",
            data={
                "calls": recent_calls,
                "user_id": user_id,
                "limit": limit
            }
        )
    except Exception as e:
        logger.exception(f"获取审计调用记录异常: {str(e)}")
        raise HTTPException(status_code=500, detail=f"获取调用记录失败: {str(e)}")


@app.get("/api/users/profile")
async def get_user_profile(user_info: dict = Depends(verify_token)):
    """
    获取当前用户的配置信息
    
    返回:
        dict: 用户配置信息
    """
    try:
        # 移除敏感信息
        safe_user_info = user_info.copy()
        if "api_key" in safe_user_info:
            safe_user_info["api_key"] = user_manager._mask_api_key(safe_user_info["api_key"])
        
        return TranscribeResponse(
            code=200,
            message="获取用户配置成功",
            data={
                "user_info": safe_user_info,
                "is_multi_user_mode": user_manager.is_multi_user_mode()
            }
        )
    except Exception as e:
        logger.exception(f"获取用户配置异常: {str(e)}")
        raise HTTPException(status_code=500, detail=f"获取用户配置失败: {str(e)}")


def start_server():
    """启动API服务器"""
    host = config.get("api", {}).get("host", "0.0.0.0")
    port = config.get("api", {}).get("port", 8000)
    
    logger.info(f"启动API服务器: {host}:{port}")
    uvicorn.run(app, host=host, port=port) 