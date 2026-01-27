import os
import json
import sqlite3
import datetime
import uuid
import secrets
from pathlib import Path
from typing import Optional, Dict, Any, List, Union
from contextlib import contextmanager
import threading
from ..logging import setup_logger

logger = setup_logger("cache_manager")


class CacheManager:
    """
    管理视频转录缓存的类
    使用 SQLite 数据库存储元数据，文件系统存储实际内容
    """
    
    def __init__(self, cache_dir: str = "./data/cache", db_path: str = None):
        """
        初始化缓存管理器
        
        Args:
            cache_dir: 缓存文件目录
            db_path: SQLite 数据库路径，默认为 cache_dir/cache.db
        """
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        
        if db_path is None:
            self.db_path = self.cache_dir / "cache.db"
        else:
            self.db_path = Path(db_path)
            
        # 使用线程本地存储来管理数据库连接
        self._local = threading.local()
        
        # 初始化数据库
        self._init_database()
        
    def _get_connection(self) -> sqlite3.Connection:
        """获取当前线程的数据库连接"""
        if not hasattr(self._local, 'connection'):
            self._local.connection = sqlite3.connect(str(self.db_path))
            self._local.connection.row_factory = sqlite3.Row
        return self._local.connection
        
    @contextmanager
    def _get_cursor(self):
        """获取数据库游标的上下文管理器"""
        conn = self._get_connection()
        cursor = conn.cursor()
        try:
            yield cursor
            conn.commit()
        except Exception as e:
            conn.rollback()
            logger.error(f"数据库操作失败: {e}")
            raise
        finally:
            cursor.close()
            
    def _init_database(self):
        """初始化数据库表结构"""
        with self._get_cursor() as cursor:
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS video_cache (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    platform TEXT NOT NULL,
                    url TEXT NOT NULL,
                    title TEXT,
                    author TEXT,
                    description TEXT,
                    media_id TEXT NOT NULL,
                    use_speaker_recognition BOOLEAN NOT NULL DEFAULT 0,
                    files_loc TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(platform, media_id, use_speaker_recognition)
                )
            ''')
            
            # 新增任务状态表
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS task_status (
                    task_id TEXT PRIMARY KEY,
                    view_token TEXT NOT NULL,
                    url TEXT NOT NULL,
                    source_url TEXT,
                    platform TEXT,
                    media_id TEXT,
                    use_speaker_recognition BOOLEAN DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'queued',
                    title TEXT,
                    author TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    completed_at TIMESTAMP,
                    cache_id INTEGER,
                    llm_config TEXT,
                    FOREIGN KEY (cache_id) REFERENCES video_cache(id)
                )
            ''')
            
            # 创建索引以提高查询性能
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_platform_media_id ON video_cache(platform, media_id)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_url ON video_cache(url)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_view_token ON task_status(view_token)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_task_status ON task_status(status)')
            
        # 执行数据库迁移
        self._migrate_database()
    
    def _migrate_database(self):
        """执行数据库迁移"""
        try:
            with self._get_cursor() as cursor:
                # 获取表的创建SQL语句
                cursor.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='task_status'")
                result = cursor.fetchone()

                if not result:
                    logger.debug("task_status 表不存在，跳过迁移")
                    return

                table_sql = result[0]

                # 迁移1: 移除view_token UNIQUE约束
                if 'UNIQUE' in table_sql and 'view_token' in table_sql:
                    logger.info("检测到view_token UNIQUE约束，开始数据库迁移...")
                    self._rebuild_task_status_table(cursor)
                    return

                # 迁移2: 添加 llm_config 字段
                cursor.execute("PRAGMA table_info(task_status)")
                columns = [col[1] for col in cursor.fetchall()]

                if 'llm_config' not in columns:
                    logger.info("添加 llm_config 字段到 task_status 表...")
                    cursor.execute("ALTER TABLE task_status ADD COLUMN llm_config TEXT")
                    logger.info("llm_config 字段添加成功")

                # 迁移3: 添加 source_url 字段
                cursor.execute("PRAGMA table_info(task_status)")
                columns = [col[1] for col in cursor.fetchall()]

                if 'source_url' not in columns:
                    logger.info("添加 source_url 字段到 task_status 表...")
                    cursor.execute("ALTER TABLE task_status ADD COLUMN source_url TEXT")
                    logger.info("source_url 字段添加成功")
                else:
                    logger.debug("数据库结构正常，无需迁移")

        except Exception as e:
            logger.error(f"数据库迁移失败: {e}")
            # 迁移失败不应该影响程序运行

    def _rebuild_task_status_table(self, cursor):
        """重建 task_status 表（用于移除 UNIQUE 约束）"""
        # 备份现有数据
        cursor.execute("SELECT * FROM task_status")
        existing_data = cursor.fetchall()

        # 获取原表列数
        cursor.execute("PRAGMA table_info(task_status)")
        old_columns = cursor.fetchall()
        old_column_count = len(old_columns)

        # 删除旧表
        cursor.execute("DROP TABLE task_status")

        # 重新创建表（包含 llm_config 和 source_url 字段）
        cursor.execute('''
            CREATE TABLE task_status (
                task_id TEXT PRIMARY KEY,
                view_token TEXT NOT NULL,
                url TEXT NOT NULL,
                source_url TEXT,
                platform TEXT,
                media_id TEXT,
                use_speaker_recognition BOOLEAN DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'queued',
                title TEXT,
                author TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                completed_at TIMESTAMP,
                cache_id INTEGER,
                llm_config TEXT,
                FOREIGN KEY (cache_id) REFERENCES video_cache(id)
            )
        ''')

        # 重新创建索引
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_view_token ON task_status(view_token)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_task_status ON task_status(status)')

        # 恢复数据（处理列数差异）
        for row in existing_data:
            row_data = list(row)

            # 处理不同版本的表结构
            if old_column_count == 12:
                # 旧表格式: task_id, view_token, url, platform, media_id, use_speaker_recognition,
                #           status, title, author, created_at, completed_at, cache_id
                # 需要在 url 后插入 source_url(None)，最后添加 llm_config(None)
                new_row_data = row_data[:3] + [None] + row_data[3:] + [None]
            elif old_column_count == 13:
                # 中间版本: 已有 llm_config 但没有 source_url
                # 需要在 url 后插入 source_url(None)
                new_row_data = row_data[:3] + [None] + row_data[3:]
            else:
                # 已经是最新版本
                new_row_data = row_data

            cursor.execute('''
                INSERT INTO task_status
                (task_id, view_token, url, source_url, platform, media_id, use_speaker_recognition,
                 status, title, author, created_at, completed_at, cache_id, llm_config)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', new_row_data)

        logger.info(f"数据库迁移完成，恢复了 {len(existing_data)} 条记录")
            
    def _get_file_path(self, platform: str, media_id: str, date: datetime.datetime = None) -> Path:
        """
        获取文件存储路径
        
        Args:
            platform: 平台名称
            media_id: 媒体ID
            date: 日期，默认为当前日期
            
        Returns:
            Path: 文件存储路径
        """
        if date is None:
            date = datetime.datetime.now()
            
        year = date.strftime("%Y")
        year_month = date.strftime("%Y%m")
        
        # 构建路径：cache_dir/platform/YYYY/YYYYMM/media_id
        file_path = self.cache_dir / platform / year / year_month / media_id
        return file_path
        
    def save_cache(self,
                   platform: str,
                   url: str,
                   media_id: str,
                   use_speaker_recognition: bool,
                   transcript_data: Any,
                   transcript_type: str,
                   title: str = "",
                   author: str = "",
                   description: str = "",
                   extra_json_data: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
        """
        保存缓存

        Args:
            platform: 平台名称
            url: 视频URL
            media_id: 媒体ID
            use_speaker_recognition: 是否使用说话人识别
            transcript_data: 转录数据
            transcript_type: 转录类型 (funasr/capswriter)
            title: 视频标题
            author: 作者
            description: 描述
            extra_json_data: 额外的JSON数据（用于CapsWriter的FunASR兼容格式）

        Returns:
            Dict: 包含缓存信息的字典
        """
        try:
            # 获取文件存储路径
            file_path = self._get_file_path(platform, media_id)
            file_path.mkdir(parents=True, exist_ok=True)

            # 保存转录文件
            if transcript_type == "funasr":
                transcript_file = file_path / "transcript_funasr.json"
                with open(transcript_file, 'w', encoding='utf-8') as f:
                    json.dump(transcript_data, f, ensure_ascii=False, indent=2)
            else:
                transcript_file = file_path / "transcript_capswriter.txt"
                with open(transcript_file, 'w', encoding='utf-8') as f:
                    f.write(transcript_data)

                # 如果提供了额外的JSON数据（CapsWriter的FunASR兼容格式），也保存
                if extra_json_data:
                    json_file = file_path / "transcript_capswriter.json"
                    with open(json_file, 'w', encoding='utf-8') as f:
                        json.dump(extra_json_data, f, ensure_ascii=False, indent=2)
                    logger.info(f"已保存 CapsWriter FunASR 兼容格式: {json_file}")
                    
            # 计算相对路径（兼容 Windows 和 Linux）
            relative_path = file_path.relative_to(self.cache_dir)
            files_loc = relative_path.as_posix()  # 使用 POSIX 格式保证跨平台兼容
            
            # 保存到数据库
            with self._get_cursor() as cursor:
                cursor.execute('''
                    INSERT OR REPLACE INTO video_cache 
                    (platform, url, title, author, description, media_id, use_speaker_recognition, files_loc, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ''', (platform, url, title, author, description, media_id, use_speaker_recognition, files_loc))
                
            logger.info(f"缓存保存成功: {platform}/{media_id}, 说话人识别: {use_speaker_recognition}")
            
            return {
                "platform": platform,
                "media_id": media_id,
                "files_loc": files_loc,
                "transcript_file": str(transcript_file)
            }
            
        except Exception as e:
            logger.error(f"保存缓存失败: {e}")
            return None
            
    def get_cache(self, 
                  platform: str = None, 
                  media_id: str = None,
                  url: str = None,
                  use_speaker_recognition: Optional[bool] = None) -> Optional[Dict[str, Any]]:
        """
        获取缓存
        
        Args:
            platform: 平台名称
            media_id: 媒体ID
            url: 视频URL
            use_speaker_recognition: 是否需要说话人识别的缓存
            
        Returns:
            Dict: 缓存数据，包含数据库记录和文件内容
        """
        try:
            with self._get_cursor() as cursor:
                # 构建查询条件
                conditions = []
                params = []
                
                if platform and media_id:
                    conditions.append("platform = ? AND media_id = ?")
                    params.extend([platform, media_id])
                elif url:
                    conditions.append("url = ?")
                    params.append(url)
                else:
                    logger.warning("必须提供 platform+media_id 或 url")
                    return None
                    
                # 处理 use_speaker_recognition 条件
                if use_speaker_recognition is True:
                    # 如果明确要求说话人识别，只查找带说话人识别的
                    conditions.append("use_speaker_recognition = 1")
                elif use_speaker_recognition is False:
                    # 如果不要求说话人识别，可以使用任何缓存（优先使用带说话人识别的）
                    pass  # 不添加条件
                    
                query = f"SELECT * FROM video_cache WHERE {' AND '.join(conditions)} ORDER BY use_speaker_recognition DESC, updated_at DESC LIMIT 1"
                cursor.execute(query, params)
                
                row = cursor.fetchone()
                if not row:
                    return None
                    
                # 转换为字典
                cache_data = dict(row)
                
                # 获取文件路径
                files_loc = Path(cache_data['files_loc'])
                file_path = self.cache_dir / files_loc
                
                # 检查文件夹是否存在
                if not file_path.exists():
                    logger.warning(f"缓存文件夹不存在: {file_path}")
                    # 删除数据库记录
                    cursor.execute("DELETE FROM video_cache WHERE id = ?", (cache_data['id'],))
                    return None
                    
                # 读取转录文件
                transcript_funasr = file_path / "transcript_funasr.json"
                transcript_capswriter = file_path / "transcript_capswriter.txt"
                
                if transcript_funasr.exists():
                    with open(transcript_funasr, 'r', encoding='utf-8') as f:
                        cache_data['transcript_data'] = json.load(f)
                    cache_data['transcript_type'] = 'funasr'
                elif transcript_capswriter.exists():
                    with open(transcript_capswriter, 'r', encoding='utf-8') as f:
                        cache_data['transcript_data'] = f.read()
                    cache_data['transcript_type'] = 'capswriter'
                else:
                    logger.warning(f"未找到转录文件: {file_path}")
                    # 删除数据库记录
                    cursor.execute("DELETE FROM video_cache WHERE id = ?", (cache_data['id'],))
                    logger.info(f"已删除无效的缓存记录: {cache_data['id']}")
                    return None
                    
                # 读取其他文件（如果存在）
                llm_calibrated = file_path / "llm_calibrated.txt"
                llm_summary = file_path / "llm_summary.txt"
                
                if llm_calibrated.exists():
                    with open(llm_calibrated, 'r', encoding='utf-8') as f:
                        cache_data['llm_calibrated'] = f.read()
                        
                if llm_summary.exists():
                    with open(llm_summary, 'r', encoding='utf-8') as f:
                        cache_data['llm_summary'] = f.read()
                        
                cache_data['file_path'] = str(file_path)
                
                logger.info(f"缓存命中: {platform}/{media_id}, 说话人识别: {cache_data['use_speaker_recognition']}")
                return cache_data
                
        except Exception as e:
            logger.error(f"获取缓存失败: {e}")
            return None
            
    def save_llm_result(self,
                        platform: str,
                        media_id: str,
                        use_speaker_recognition: bool,
                        llm_type: str,
                        content: Union[str, Dict]) -> bool:
        """
        保存 LLM 处理结果

        Args:
            platform: 平台名称
            media_id: 媒体ID
            use_speaker_recognition: 是否使用了说话人识别
            llm_type: LLM 类型 (calibrated/summary/structured)
            content: LLM 处理后的内容
                - calibrated/summary: 纯文本字符串
                - structured: 结构化数据字典（自动添加 format_version）

        Returns:
            bool: 是否保存成功
        """
        try:
            # 先获取缓存记录
            cache_data = self.get_cache(platform, media_id, use_speaker_recognition=use_speaker_recognition)
            if not cache_data:
                logger.warning(f"未找到缓存记录: {platform}/{media_id}")
                return False

            # 获取文件路径
            file_path = Path(cache_data['file_path'])

            # 保存 LLM 结果
            if llm_type == "calibrated":
                llm_file = file_path / "llm_calibrated.txt"
                with open(llm_file, 'w', encoding='utf-8') as f:
                    f.write(content)

            elif llm_type == "summary":
                llm_file = file_path / "llm_summary.txt"
                with open(llm_file, 'w', encoding='utf-8') as f:
                    f.write(content)

            elif llm_type == "structured":
                # 保存结构化数据到 JSON 文件
                llm_file = file_path / "llm_processed.json"

                # 确保是字典类型
                if not isinstance(content, dict):
                    logger.error(f"structured 类型要求 content 为字典，实际类型: {type(content)}")
                    return False

                # 添加格式版本标记（兼容旧渲染器）
                structured_data = {
                    "format_version": "v2",
                    **content  # 合并传入的结构化数据
                }

                with open(llm_file, 'w', encoding='utf-8') as f:
                    json.dump(structured_data, f, ensure_ascii=False, indent=2)

            else:
                logger.error(f"未知的 LLM 类型: {llm_type}")
                return False

            logger.info(f"LLM 结果保存成功: {platform}/{media_id}/{llm_type}")
            return True

        except Exception as e:
            logger.error(f"保存 LLM 结果失败: {e}")
            return False
            
    def list_cache(self, 
                   platform: str = None,
                   limit: int = 100,
                   offset: int = 0) -> List[Dict[str, Any]]:
        """
        列出缓存记录
        
        Args:
            platform: 平台名称（可选）
            limit: 返回记录数限制
            offset: 偏移量
            
        Returns:
            List[Dict]: 缓存记录列表
        """
        try:
            with self._get_cursor() as cursor:
                if platform:
                    query = "SELECT * FROM video_cache WHERE platform = ? ORDER BY updated_at DESC LIMIT ? OFFSET ?"
                    cursor.execute(query, (platform, limit, offset))
                else:
                    query = "SELECT * FROM video_cache ORDER BY updated_at DESC LIMIT ? OFFSET ?"
                    cursor.execute(query, (limit, offset))
                    
                rows = cursor.fetchall()
                return [dict(row) for row in rows]
                
        except Exception as e:
            logger.error(f"列出缓存失败: {e}")
            return []
            
    def get_cache_stats(self) -> Dict[str, Any]:
        """
        获取缓存统计信息
        
        Returns:
            Dict: 统计信息
        """
        try:
            with self._get_cursor() as cursor:
                # 总记录数
                cursor.execute("SELECT COUNT(*) as total FROM video_cache")
                total = cursor.fetchone()['total']
                
                # 按平台统计
                cursor.execute("""
                    SELECT platform, COUNT(*) as count 
                    FROM video_cache 
                    GROUP BY platform
                """)
                platform_stats = {row['platform']: row['count'] for row in cursor.fetchall()}
                
                # 按说话人识别统计
                cursor.execute("""
                    SELECT use_speaker_recognition, COUNT(*) as count 
                    FROM video_cache 
                    GROUP BY use_speaker_recognition
                """)
                speaker_stats = {bool(row['use_speaker_recognition']): row['count'] for row in cursor.fetchall()}
                
                # 计算缓存目录大小
                cache_size = sum(f.stat().st_size for f in self.cache_dir.rglob('*') if f.is_file())
                
                return {
                    "total_records": total,
                    "platform_stats": platform_stats,
                    "speaker_recognition_stats": speaker_stats,
                    "cache_size_mb": round(cache_size / 1024 / 1024, 2)
                }
                
        except Exception as e:
            logger.error(f"获取缓存统计失败: {e}")
            return {}
            
    def validate_cache_integrity(self) -> int:
        """
        验证缓存完整性，删除文件不存在的记录
        
        Returns:
            int: 删除的无效记录数
        """
        try:
            deleted_count = 0
            
            with self._get_cursor() as cursor:
                # 获取所有缓存记录
                cursor.execute("SELECT id, files_loc FROM video_cache")
                records = cursor.fetchall()
                
                invalid_records = []
                
                for record in records:
                    files_loc = Path(record['files_loc'])
                    file_path = self.cache_dir / files_loc
                    
                    # 检查文件夹是否存在
                    if not file_path.exists():
                        invalid_records.append(record['id'])
                        logger.warning(f"缓存文件夹不存在: {file_path}")
                        continue
                    
                    # 检查转录文件是否存在
                    transcript_funasr = file_path / "transcript_funasr.json"
                    transcript_capswriter = file_path / "transcript_capswriter.txt"
                    
                    if not transcript_funasr.exists() and not transcript_capswriter.exists():
                        invalid_records.append(record['id'])
                        logger.warning(f"转录文件不存在: {file_path}")
                
                # 批量删除无效记录
                if invalid_records:
                    placeholders = ','.join(['?'] * len(invalid_records))
                    cursor.execute(f"DELETE FROM video_cache WHERE id IN ({placeholders})", invalid_records)
                    deleted_count = len(invalid_records)
                    logger.info(f"批量删除了 {deleted_count} 条无效缓存记录")
                
                return deleted_count
                
        except Exception as e:
            logger.error(f"验证缓存完整性失败: {e}")
            return 0

    def cleanup_old_cache(self, days: int = 30) -> int:
        """
        清理旧缓存
        
        Args:
            days: 保留最近几天的缓存
            
        Returns:
            int: 删除的记录数
        """
        try:
            cutoff_date = datetime.datetime.now() - datetime.timedelta(days=days)
            
            with self._get_cursor() as cursor:
                # 获取要删除的记录
                cursor.execute("""
                    SELECT id, files_loc 
                    FROM video_cache 
                    WHERE updated_at < ?
                """, (cutoff_date,))
                
                records_to_delete = cursor.fetchall()
                
                # 删除文件
                for record in records_to_delete:
                    file_path = self.cache_dir / Path(record['files_loc'])
                    if file_path.exists():
                        import shutil
                        shutil.rmtree(file_path)
                        
                # 删除数据库记录
                cursor.execute("DELETE FROM video_cache WHERE updated_at < ?", (cutoff_date,))
                
                deleted_count = len(records_to_delete)
                logger.info(f"清理了 {deleted_count} 条旧缓存记录")
                return deleted_count
                
        except Exception as e:
            logger.error(f"清理缓存失败: {e}")
            return 0
            
    def close(self):
        """关闭数据库连接"""
        if hasattr(self._local, 'connection'):
            self._local.connection.close()
    
    # ========== 任务状态管理方法 ==========
    
    def generate_task_id(self) -> str:
        """生成全局唯一任务ID"""
        return f"task_{uuid.uuid4().hex}"
    
    def generate_view_token(self) -> str:
        """生成查看token"""
        return f"view_{secrets.token_urlsafe(32)}"
    
    def create_task(self, url: str, use_speaker_recognition: bool = False, source_url: str = None) -> Dict[str, str]:
        """
        创建新任务，相同URL会复用view_token

        Args:
            url: 视频URL（实际下载地址）
            use_speaker_recognition: 是否使用说话人识别
            source_url: 原始平台URL（用于显示，可选）

        Returns:
            Dict: 包含task_id和view_token的字典
        """
        task_id = self.generate_task_id()

        # 将空字符串转换为 None，避免存储无意义的空字符串
        if source_url is not None and not source_url.strip():
            source_url = None

        # 检查是否已有相同URL的任务，如果有则复用其view_token（无论任务状态）
        existing_task = self.get_existing_task_by_url(url, use_speaker_recognition)
        if existing_task:
            view_token = existing_task['view_token']
            logger.info(f"复用现有view_token: {view_token} (状态: {existing_task['status']}) for URL: {url}")
        else:
            view_token = self.generate_view_token()
            logger.info(f"生成新view_token: {view_token} for URL: {url}")

        try:
            with self._get_cursor() as cursor:
                cursor.execute('''
                    INSERT INTO task_status
                    (task_id, view_token, url, source_url, use_speaker_recognition, status)
                    VALUES (?, ?, ?, ?, ?, 'queued')
                ''', (task_id, view_token, url, source_url, use_speaker_recognition))

            logger.info(f"任务创建成功: {task_id}, view_token: {view_token}, source_url: {source_url}")
            return {
                "task_id": task_id,
                "view_token": view_token
            }
        except Exception as e:
            logger.error(f"创建任务失败: {e}")
            raise
    
    def update_task_status(self, task_id: str, status: str, platform: str = None,
                          media_id: str = None, title: str = None, author: str = None,
                          cache_id: int = None, source_url: str = None):
        """
        更新任务状态

        Args:
            task_id: 任务ID
            status: 状态 (queued/processing/success/failed)
            platform: 平台名称
            media_id: 媒体ID
            title: 视频标题
            author: 作者
            cache_id: 关联的缓存ID
            source_url: 原始平台URL
        """
        try:
            # 将空字符串转换为 None，避免存储无意义的空字符串
            if source_url is not None and isinstance(source_url, str) and not source_url.strip():
                source_url = None

            with self._get_cursor() as cursor:
                # 构建更新语句
                update_fields = ["status = ?"]
                params = [status]

                if platform:
                    update_fields.append("platform = ?")
                    params.append(platform)
                if media_id:
                    update_fields.append("media_id = ?")
                    params.append(media_id)
                if title:
                    update_fields.append("title = ?")
                    params.append(title)
                if author:
                    update_fields.append("author = ?")
                    params.append(author)
                if cache_id:
                    update_fields.append("cache_id = ?")
                    params.append(cache_id)
                if source_url is not None:  # 只有非空的 source_url 才更新
                    update_fields.append("source_url = ?")
                    params.append(source_url)

                if status in ['success', 'failed']:
                    update_fields.append("completed_at = CURRENT_TIMESTAMP")

                params.append(task_id)

                query = f"UPDATE task_status SET {', '.join(update_fields)} WHERE task_id = ?"
                cursor.execute(query, params)

                logger.info(f"任务状态更新: {task_id} -> {status}")

        except Exception as e:
            logger.error(f"更新任务状态失败: {e}")
            raise
    
    def get_task_by_id(self, task_id: str) -> Optional[Dict[str, Any]]:
        """
        根据任务ID获取任务信息
        
        Args:
            task_id: 任务ID
            
        Returns:
            Dict: 任务信息
        """
        try:
            with self._get_cursor() as cursor:
                cursor.execute("SELECT * FROM task_status WHERE task_id = ?", (task_id,))
                row = cursor.fetchone()
                
                if row:
                    return dict(row)
                return None
                
        except Exception as e:
            logger.error(f"获取任务信息失败: {e}")
            return None
    
    def get_task_by_view_token(self, view_token: str) -> Optional[Dict[str, Any]]:
        """
        根据view_token获取任务信息

        优先返回成功状态的任务，如果有多个任务则返回最新的

        Args:
            view_token: 查看token

        Returns:
            Dict: 任务信息
        """
        try:
            with self._get_cursor() as cursor:
                # 优先返回成功状态的任务，其次返回最新的任务
                cursor.execute("""
                    SELECT * FROM task_status
                    WHERE view_token = ?
                    ORDER BY
                        CASE status
                            WHEN 'success' THEN 1
                            WHEN 'processing' THEN 2
                            WHEN 'queued' THEN 3
                            WHEN 'failed' THEN 4
                            ELSE 5
                        END,
                        created_at DESC
                    LIMIT 1
                """, (view_token,))
                row = cursor.fetchone()

                if row:
                    return dict(row)
                return None

        except Exception as e:
            logger.error(f"根据view_token获取任务信息失败: {e}")
            return None
    
    def get_view_data_by_token(self, view_token: str) -> Optional[Dict[str, Any]]:
        """
        根据view_token获取查看页面数据
        
        Args:
            view_token: 查看token
            
        Returns:
            Dict: 页面数据
        """
        try:
            # 获取任务信息
            task_info = self.get_task_by_view_token(view_token)
            if not task_info:
                return None

            # 优先使用 source_url（如果存在且非空），否则回退到 url
            source_url_value = task_info.get('source_url')
            display_url = source_url_value if (source_url_value and source_url_value.strip()) else task_info['url']

            # 如果任务还在进行中
            if task_info['status'] in ['queued', 'processing']:
                return {
                    'status': 'processing',
                    'title': task_info.get('title', '转录处理中...'),
                    'url': display_url,
                    'created_at': task_info['created_at']
                }

            # 如果任务失败
            if task_info['status'] == 'failed':
                return {
                    'status': 'failed',
                    'title': task_info.get('title', '转录失败'),
                    'url': display_url,
                    'message': '转录任务失败，请重新提交'
                }
            
            # 任务成功，获取缓存数据
            if task_info['platform'] and task_info['media_id']:
                cache_data = self.get_cache(
                    platform=task_info['platform'],
                    media_id=task_info['media_id'],
                    use_speaker_recognition=task_info['use_speaker_recognition']
                )
                
                if cache_data:
                    # 缓存存在，返回完整数据
                    # 确保返回的是字符串类型
                    summary = cache_data.get('llm_summary', '总结处理中...')
                    if not isinstance(summary, str):
                        summary = str(summary) if summary is not None else '总结处理中...'

                    transcript = cache_data.get('llm_calibrated') or cache_data.get('transcript_data', '转录文本获取中...')
                    if not isinstance(transcript, str):
                        transcript = str(transcript) if transcript is not None else '转录文本获取中...'

                    # 获取 LLM 模型配置
                    llm_config = self.get_task_llm_config(task_info['task_id'])

                    return {
                        'status': 'success',
                        'title': cache_data.get('title', ''),
                        'author': cache_data.get('author', ''),
                        'description': cache_data.get('description', ''),
                        'url': display_url,
                        'summary': summary,
                        'transcript': transcript,
                        'use_speaker_recognition': cache_data.get('use_speaker_recognition', False),
                        'created_at': task_info['created_at'],
                        'cache_dir': cache_data.get('file_path'),
                        'llm_config': llm_config,  # 添加 LLM 模型配置
                        'platform': cache_data.get('platform', '')
                    }
                else:
                    # 底层文件已清理
                    return {
                        'status': 'file_cleaned',
                        'title': task_info.get('title', '视频转录'),
                        'url': display_url,
                        'created_at': task_info['created_at']
                    }
            else:
                # 任务信息不完整
                return {
                    'status': 'incomplete',
                    'title': task_info.get('title', '任务信息不完整'),
                    'url': display_url,
                    'created_at': task_info['created_at']
                }
                
        except Exception as e:
            logger.error(f"获取查看页面数据失败: {e}")
            return None
    
    def get_existing_task_by_url(self, url: str, use_speaker_recognition: bool = False) -> Optional[Dict[str, Any]]:
        """
        根据URL和说话人识别参数查找现有任务
        
        Args:
            url: 视频URL
            use_speaker_recognition: 是否使用说话人识别
            
        Returns:
            Optional[Dict]: 现有任务信息（包含task_id和view_token），如果没有找到则返回None
        """
        try:
            with self._get_cursor() as cursor:
                # 查找相同URL和说话人识别设置的任务
                # 优先返回成功完成的任务，其次是处理中的任务
                cursor.execute('''
                    SELECT task_id, view_token, url, source_url, use_speaker_recognition, status,
                           title, author, platform, media_id, cache_id, created_at
                    FROM task_status
                    WHERE url = ? AND use_speaker_recognition = ?
                    ORDER BY
                        CASE status
                            WHEN 'success' THEN 1
                            WHEN 'processing' THEN 2
                            WHEN 'queued' THEN 3
                            ELSE 4
                        END,
                        created_at DESC
                    LIMIT 1
                ''', (url, use_speaker_recognition))

                row = cursor.fetchone()
                if row:
                    task_info = {
                        'task_id': row[0],
                        'view_token': row[1],
                        'url': row[2],
                        'source_url': row[3],
                        'use_speaker_recognition': bool(row[4]),
                        'status': row[5],
                        'title': row[6],
                        'author': row[7],
                        'platform': row[8],
                        'media_id': row[9],
                        'cache_id': row[10],
                        'created_at': row[11]
                    }
                    logger.info(f"找到现有任务: {task_info['task_id']}, 状态: {task_info['status']}, URL: {url}")
                    return task_info
                else:
                    logger.debug(f"未找到现有任务: URL={url}, use_speaker_recognition={use_speaker_recognition}")
                    return None

        except Exception as e:
            logger.error(f"查找现有任务失败: {e}")
            return None

    def update_task_llm_config(self, task_id: str, llm_config: Dict[str, Any]) -> bool:
        """
        更新任务的 LLM 模型配置信息

        Args:
            task_id: 任务ID
            llm_config: LLM 模型配置字典，包含:
                - calibrate_model: 校对模型
                - calibrate_reasoning_effort: 校对模型推理强度
                - summary_model: 总结模型
                - summary_reasoning_effort: 总结模型推理强度
                - validator_model: 校验模型
                - validator_reasoning_effort: 校验模型推理强度
                - risk_detected: 是否检测到风险内容
                - recorded_at: 记录时间

        Returns:
            bool: 是否更新成功
        """
        try:
            # 添加记录时间
            if 'recorded_at' not in llm_config:
                llm_config['recorded_at'] = datetime.datetime.now().isoformat()

            llm_config_json = json.dumps(llm_config, ensure_ascii=False)

            with self._get_cursor() as cursor:
                cursor.execute(
                    "UPDATE task_status SET llm_config = ? WHERE task_id = ?",
                    (llm_config_json, task_id)
                )

                if cursor.rowcount > 0:
                    logger.info(f"LLM配置已保存: task_id={task_id}, risk_detected={llm_config.get('risk_detected', False)}")
                    return True
                else:
                    logger.warning(f"未找到任务: {task_id}")
                    return False

        except Exception as e:
            logger.error(f"更新LLM配置失败: {e}")
            return False

    def get_task_llm_config(self, task_id: str) -> Optional[Dict[str, Any]]:
        """
        获取任务的 LLM 模型配置信息

        Args:
            task_id: 任务ID

        Returns:
            Dict: LLM 模型配置字典，如果不存在则返回 None
        """
        try:
            with self._get_cursor() as cursor:
                cursor.execute(
                    "SELECT llm_config FROM task_status WHERE task_id = ?",
                    (task_id,)
                )
                row = cursor.fetchone()

                if row and row['llm_config']:
                    return json.loads(row['llm_config'])
                return None

        except Exception as e:
            logger.error(f"获取LLM配置失败: {e}")
            return None
