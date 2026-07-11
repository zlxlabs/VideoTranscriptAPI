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
from ..utils.logging import setup_logger
from ..utils.task_status import TaskStatus
from ..utils.llm_status import SummaryStatus

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

        # 按 (platform, media_id) 粒度的进程内锁池，保护缓存产物（llm_status.json
        # 的读-改-写，以及分层缓存"层是否已存在"的判定 + 写入，见 media_lock()
        # 文档）不被跨任务并发踩踏。用 RLock 而不是 Lock：调用方（例如
        # llm_ops._save_llm_results）可能持锁写入产物后，内部再调用本类的
        # save_llm_status()，其内部也会请求同一把锁——同一线程必须能重入，
        # 否则会自锁死锁。
        # 懒创建 + 引用计数弹出，实现模式与 api/context.py 的 task_lock 池
        # 一致，但弹出判断改用引用计数而非"释放后检查 locked()"——后者存在
        # 生命周期竞态（codex-review R3 #2）：等待线程已经从字典里取出锁
        # 对象、但还未真正调用 acquire() 的窗口内，持有者释放锁后见
        # `not lock.locked()`（此时确实没人持有）就把锁从字典弹出；第三个
        # 线程随后为同一个 key 新建一把不同的锁对象，导致等待线程（仍持有
        # 旧锁对象的引用）和第三个线程（持有新锁对象）可以同时进入本应互斥
        # 的临界区。引用计数在"从字典取出锁对象"的同一个 guard 临界区内
        # +1（严格早于 acquire()，因此不存在"已取到对象但计数未体现"的
        # 窗口），释放锁之后再 -1，归零才真正弹出，从根上消除这个窗口。
        # 注：threading.RLock 在本项目所用 Python 版本上没有 locked() 方法，
        # 这也是弹出判断必须用引用计数、不能用 locked() 的另一个原因。
        self._media_locks: Dict[str, threading.RLock] = {}
        self._media_lock_refcounts: Dict[str, int] = {}
        self._media_locks_guard = threading.Lock()

        # 初始化数据库
        self._init_database()
        
    def _get_connection(self) -> sqlite3.Connection:
        """获取当前线程的数据库连接"""
        if not hasattr(self._local, 'connection'):
            self._local.connection = sqlite3.connect(str(self.db_path))
            self._local.connection.row_factory = sqlite3.Row
            # 启用 WAL 模式提升并发读写性能
            try:
                self._local.connection.execute("PRAGMA journal_mode=WAL")
            except sqlite3.OperationalError:
                logger.warning("WAL mode not supported, using default journal mode")
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
            
    @contextmanager
    def media_lock(self, platform: str, media_id: str):
        """按 (platform, media_id) 粒度加锁的上下文管理器（公开 API）。

        用于保护同一媒体的缓存产物读-改-写不被并发任务踩踏——task_lock
        （api/context.py）是按 task_id 加锁的，锁不住"两个不同 task_id、
        但操作同一份媒体缓存"这种跨任务场景。两类已知场景：

        1. llm_status.json 的读-改-写（本类内部使用，见 save_llm_status）：
           例如一个任务只补校对、另一个任务只补总结，二者并发调用
           save_llm_status 时若无锁，读-改-写语义下后写者会用自己读到的
           旧快照覆盖先写者刚写入的字段。
        2. 分层缓存"层是否已存在"的判定与写入（llm_ops._save_llm_results
           使用，见该函数文档，codex-review R3 #1）：任务 A 请求的处理
           深度不含某一层，需要靠"该层此前是否已存在"决定是否抑制写入；
           这个判定 + 写入必须与另一个并发任务对同一层的真实写入互斥，
           否则 A 会拿着写入前拍下的旧快照，在写入前那一刻把已经被写入
           的真实产物覆盖掉。

        公开为 media_lock（而非内部私有方法）供 llm_ops 等上层模块直接
        使用，把"判定 -> 写入 -> 合并状态"整段纳入同一把锁的保护范围。

        用 RLock：调用方可能已经持有这把锁（例如 llm_ops._save_llm_results
        持锁写入产物后，最终会调用本类的 save_llm_status，其内部也会请求
        同一把锁）——RLock 允许同一线程重入，避免这种调用链自锁死锁。

        实现模式与 api/context.py 的 task_lock 基本一致：懒创建锁对象，
        用守护锁保护锁字典本身的读写；弹出判断用引用计数（而非释放后检查
        locked()，该写法有生命周期竞态——等待线程可能已从字典取到锁对象、
        但尚未 acquire 时，持有者释放并见"当前无人持有"就弹出，导致后续
        线程新建另一把锁对象、与仍在等待的线程各持一把锁同时进入临界区，
        codex-review R3 #2 已修复），归零才真正从字典弹出，避免锁池随
        媒体数量无限增长。
        """
        key = f"{platform}:{media_id}"
        with self._media_locks_guard:
            lock = self._media_locks.get(key)
            if lock is None:
                lock = threading.RLock()
                self._media_locks[key] = lock
                self._media_lock_refcounts[key] = 0
            # 计数必须在这个 guard 临界区内完成，且严格早于下面的
            # lock.acquire()：这样任何"晚到但已经拿到锁对象引用"的线程都
            # 会先把自己算进计数，持有者释放时才能看到这个意向，不会误判
            # 锁已空闲。
            self._media_lock_refcounts[key] += 1
        lock.acquire()
        try:
            yield
        finally:
            lock.release()
            with self._media_locks_guard:
                self._media_lock_refcounts[key] -= 1
                if self._media_lock_refcounts[key] <= 0:
                    self._media_locks.pop(key, None)
                    self._media_lock_refcounts.pop(key, None)

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
                    download_url TEXT,
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
                    error_message TEXT,
                    FOREIGN KEY (cache_id) REFERENCES video_cache(id)
                )
            ''')

            # 创建索引以提高查询性能
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_platform_media_id ON video_cache(platform, media_id)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_url ON video_cache(url)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_view_token ON task_status(view_token)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_task_status ON task_status(status)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_task_platform_media ON task_status(platform, media_id)')
            
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
                # 注意：重建后的表结构里已经内联了当前全部已知列（见
                # _rebuild_task_status_table），但这里刻意不再 return——
                # 后续迁移2-5 的 PRAGMA 存在性检查本身是幂等的（列已存在则
                # 跳过 ALTER TABLE），让它们继续跑一遍是双重保险：即使未来
                # 有人往表里加新列却忘了同步更新重建逻辑，旧库升级也不会
                # 再次丢列。历史 bug：这里曾经 return，导致重建表跳过了
                # 之后新增的 calibration_status/summary_status 字段，老库
                # 升级后 LLM 完成时 UPDATE 报 "no such column"。
                if 'UNIQUE' in table_sql and 'view_token' in table_sql:
                    logger.info("检测到view_token UNIQUE约束，开始数据库迁移...")
                    self._rebuild_task_status_table(cursor)

                # 迁移2: 添加 llm_config 字段
                cursor.execute("PRAGMA table_info(task_status)")
                columns = [col[1] for col in cursor.fetchall()]

                if 'llm_config' not in columns:
                    logger.info("添加 llm_config 字段到 task_status 表...")
                    cursor.execute("ALTER TABLE task_status ADD COLUMN llm_config TEXT")
                    logger.info("llm_config 字段添加成功")

                # 迁移3: 添加 download_url 字段
                cursor.execute("PRAGMA table_info(task_status)")
                columns = [col[1] for col in cursor.fetchall()]

                if 'download_url' not in columns:
                    logger.info("添加 download_url 字段到 task_status 表...")
                    cursor.execute("ALTER TABLE task_status ADD COLUMN download_url TEXT")
                    logger.info("download_url 字段添加成功")

                # 迁移4: 添加 error_message 字段（失败原因持久化）
                cursor.execute("PRAGMA table_info(task_status)")
                columns = [col[1] for col in cursor.fetchall()]

                if 'error_message' not in columns:
                    logger.info("添加 error_message 字段到 task_status 表...")
                    cursor.execute("ALTER TABLE task_status ADD COLUMN error_message TEXT")
                    logger.info("error_message 字段添加成功")

                # 迁移5: 添加 calibration_status / summary_status 字段（诚实状态模型）
                cursor.execute("PRAGMA table_info(task_status)")
                columns = [col[1] for col in cursor.fetchall()]

                if 'calibration_status' not in columns:
                    logger.info("添加 calibration_status 字段到 task_status 表...")
                    cursor.execute("ALTER TABLE task_status ADD COLUMN calibration_status TEXT")
                    logger.info("calibration_status 字段添加成功")

                if 'summary_status' not in columns:
                    logger.info("添加 summary_status 字段到 task_status 表...")
                    cursor.execute("ALTER TABLE task_status ADD COLUMN summary_status TEXT")
                    logger.info("summary_status 字段添加成功")

                if all(c in columns for c in ('calibration_status', 'summary_status', 'error_message')):
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

        # 重新创建表：直接内联当前全部已知列（llm_config/download_url/
        # error_message/calibration_status/summary_status），而不是只建出
        # 迁移1当时存在的那几列再指望后续迁移补齐——见 _migrate_database()
        # 里放弃提前 return 的注释，这里是同一个修复的另一半：两处任何一处
        # 单独修都不足以根治"旧库升级丢新列"的问题。
        cursor.execute('''
            CREATE TABLE task_status (
                task_id TEXT PRIMARY KEY,
                view_token TEXT NOT NULL,
                url TEXT NOT NULL,
                download_url TEXT,
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
                error_message TEXT,
                calibration_status TEXT,
                summary_status TEXT,
                FOREIGN KEY (cache_id) REFERENCES video_cache(id)
            )
        ''')

        # 重新创建索引
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_view_token ON task_status(view_token)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_task_status ON task_status(status)')

        # 恢复数据（处理列数差异）
        old_column_names = [col[1] for col in old_columns]
        for row in existing_data:
            row_data = list(row)
            row_map = {name: row_data[idx] for idx, name in enumerate(old_column_names)}

            new_row_data = [
                row_map.get("task_id"),
                row_map.get("view_token"),
                row_map.get("url"),
                row_map.get("download_url"),
                row_map.get("platform"),
                row_map.get("media_id"),
                row_map.get("use_speaker_recognition"),
                row_map.get("status"),
                row_map.get("title"),
                row_map.get("author"),
                row_map.get("created_at"),
                row_map.get("completed_at"),
                row_map.get("cache_id"),
                row_map.get("llm_config"),
                row_map.get("error_message"),
                row_map.get("calibration_status"),
                row_map.get("summary_status"),
            ]

            cursor.execute('''
                INSERT INTO task_status
                (task_id, view_token, url, download_url, platform, media_id, use_speaker_recognition,
                 status, title, author, created_at, completed_at, cache_id, llm_config,
                 error_message, calibration_status, summary_status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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

                # 读取诚实状态模型落盘文件（可能不存在：历史任务或校对未完成）
                llm_status_file = file_path / "llm_status.json"
                if llm_status_file.exists():
                    try:
                        with open(llm_status_file, 'r', encoding='utf-8') as f:
                            cache_data['llm_status'] = json.load(f)
                    except (OSError, json.JSONDecodeError) as status_exc:
                        logger.warning(f"读取 llm_status.json 失败，忽略: {status_exc}")

                # 读取说话人结构化数据（仅说话人识别路径才会有：dialogs/speaker_mapping
                # 等，见 save_llm_result(llm_type="structured")）。可能不存在：非说话人
                # 缓存、或说话人缓存尚未完成一次真实校对。调用方（如分层缓存"只补总结"
                # 场景）用它拿到真实说话人数，避免总结时因内容被降级为纯文本而误判为
                # 单说话人（codex-review R5 #3）。
                llm_processed_file = file_path / "llm_processed.json"
                if llm_processed_file.exists():
                    try:
                        with open(llm_processed_file, 'r', encoding='utf-8') as f:
                            cache_data['llm_processed'] = json.load(f)
                    except (OSError, json.JSONDecodeError) as processed_exc:
                        logger.warning(f"读取 llm_processed.json 失败，忽略: {processed_exc}")

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

                # 添加格式版本标记（用于溯源产出该缓存的校对流水线）
                # v3: ID 锚点校对契约（corrections[{id,text}]，结构为 ground truth）。
                # 注意：本字段仅作溯源标记，不参与缓存复用判定（复用 gate 是 llm_calibrated.txt 是否存在）；
                # 历史 v2 缓存为修复前文本，需对个别集手动重处理才能拿到 ID 锚点修复后的结果。
                structured_data = {
                    "format_version": "v3",
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

    def save_llm_status(
        self,
        platform: str,
        media_id: str,
        use_speaker_recognition: bool,
        calibration_status: Optional[str] = None,
        calibration_stats: Optional[Dict[str, Any]] = None,
        summary_status: Optional[str] = None,
    ) -> bool:
        """写入/合并 llm_status.json（"诚实状态模型"统一落盘文件）。

        读-改-写、按字段合并语义：只覆盖本次传入的非 None 字段，未传入的字段
        保留旧值不变。这是关键设计——recalibrate（calibrate_only=True 且未补跑
        summary）场景下，本次调用只知道新的 calibration_status，若整份覆盖会把
        已有的 summary_status（如 generated）静默抹掉，退回到"看起来总结缺失"
        的旧 bug。

        Args:
            platform: 平台名称
            media_id: 媒体ID
            use_speaker_recognition: 是否使用了说话人识别（用于定位缓存目录）
            calibration_status: CalibrationStatus 取值（full/partial/none），None 表示不更新
            calibration_stats: 校对统计详情（分段/分块数据，结构随处理器而异），None 表示不更新
            summary_status: SummaryStatus 取值（generated/skipped_short/failed/pending），
                None 表示不更新（保留旧值，见上方合并语义说明）

        Returns:
            bool: 是否保存成功（找不到对应缓存记录时返回 False）
        """
        try:
            # 全程持锁：同一媒体的读-改-写不能被另一个任务的并发调用打断，
            # 否则两个任务各自读到旧快照后先后写回，后写者会用自己那份
            # 缺字段的旧快照覆盖先写者刚合并进去的字段（见 media_lock 文档）。
            with self.media_lock(platform, media_id):
                cache_data = self.get_cache(platform, media_id, use_speaker_recognition=use_speaker_recognition)
                if not cache_data:
                    logger.warning(f"未找到缓存记录，无法写入 llm_status.json: {platform}/{media_id}")
                    return False

                file_path = Path(cache_data['file_path'])
                status_file = file_path / "llm_status.json"

                # 读取已有内容作为合并基础（不存在或损坏则视为空）
                existing: Dict[str, Any] = {}
                if status_file.exists():
                    try:
                        with open(status_file, 'r', encoding='utf-8') as f:
                            existing = json.load(f)
                    except (OSError, json.JSONDecodeError) as read_exc:
                        logger.warning(f"读取旧 llm_status.json 失败，将整份重写: {read_exc}")
                        existing = {}

                if calibration_status is not None:
                    existing['calibration_status'] = calibration_status
                if calibration_stats is not None:
                    existing['calibration_stats'] = calibration_stats
                if summary_status is not None:
                    existing['summary_status'] = summary_status
                existing['updated_at'] = datetime.datetime.now(datetime.timezone.utc).strftime(
                    '%Y-%m-%d %H:%M:%S'
                )

                # 原子写入：先写同目录下的临时文件再 os.replace 原地替换，
                # 避免并发读者（get_cache 等）在写入过程中读到半截 JSON。
                # 临时文件名带 pid+线程 id，避免罕见的"锁已释放但旧临时文件
                # 还未清理"场景下与其他写者相互冲突。
                tmp_file = status_file.with_name(
                    f"{status_file.name}.tmp{os.getpid()}_{threading.get_ident()}"
                )
                try:
                    with open(tmp_file, 'w', encoding='utf-8') as f:
                        json.dump(existing, f, ensure_ascii=False, indent=2)
                    os.replace(tmp_file, status_file)
                except Exception:
                    # 写入/替换失败时清理残留临时文件，不让半成品文件留在缓存目录里
                    try:
                        tmp_file.unlink(missing_ok=True)
                    except OSError:
                        pass
                    raise

                logger.info(f"llm_status.json 已更新: {platform}/{media_id}")
                return True

        except Exception as e:
            logger.error(f"保存 llm_status.json 失败: {e}")
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

    def cleanup_task_status(
        self, retention_days: int, cache_retention_days: Optional[int] = None
    ) -> int:
        """
        清理过期的终态任务状态记录（task_status 表）

        仅删除已进入终态（success/failed）且完成时间早于保留期的记录；
        非终态任务（queued/processing/calibrating）一律保留，避免误删仍在
        处理中、或崩溃后等待启动恢复扫描（recover_orphaned_tasks）的任务。

        view_token 保护（codex-review R3 #3）：/view/{view_token} 的解析
        链路完全依赖本表——get_view_data_by_token -> get_task_by_view_token
        -> `SELECT * FROM task_status WHERE view_token = ?`，view_token 不
        存在于 video_cache 表。因此删除终态任务行会立刻使其 view 链接失效，
        即使底层缓存产物（video_cache 行 + 文件）仍在保留期内。为维持
        "链接寿命不短于缓存寿命"的不变式，调用方传入 cache_retention_days
        时：
        - cache_retention_days > 0 且 retention_days 短于它：把生效保留期
          钳制到 cache_retention_days（取二者较大值），并记 warning；
        - cache_retention_days <= 0（缓存永久保留）：直接跳过清理并记
          warning——缓存永不过期意味着 view 链接也必须永久有效；
        - 不传（None，向后兼容旧调用方/测试）：维持原有行为，不做钳制。

        时间比较基准优先取 completed_at，若为历史遗留的 NULL（理论上
        update_task_status/recover_orphaned_tasks 在把状态写为终态的同一
        条 UPDATE 里必定同步写 completed_at = CURRENT_TIMESTAMP，此处仅作
        防御性兜底）则回退 created_at，避免这类边缘记录永远无法被回收。
        task_status 的 created_at/completed_at 均由 SQLite 的
        `CURRENT_TIMESTAMP` 写入，固定为 UTC、无时区后缀的
        "YYYY-MM-DD HH:MM:SS" 格式；这里用同样格式生成 cutoff 字符串参与
        比较，避免依赖 sqlite3 模块的隐式 datetime adapter（该 adapter 从
        Python 3.12 起已弃用），做法与 AuditLogger.cleanup_old_logs 一致。

        Args:
            retention_days: 保留天数，早于 (当前 UTC 时间 - retention_days) 的
                终态记录会被删除
            cache_retention_days: 缓存（转录产物）保留天数配置，用于上述
                view_token 保护钳制；None 表示调用方未提供、不钳制，
                0 或负数表示缓存永久保留、跳过清理

        Returns:
            int: 实际删除的记录数
        """
        try:
            if cache_retention_days is not None:
                if cache_retention_days <= 0:
                    logger.warning(
                        "task_status 清理已跳过：cache_retention_days<=0 表示缓存永久保留，"
                        "而 /view/{view_token} 链接依赖 task_status 行解析，"
                        "提前删除会造成缓存尚在、链接已死"
                    )
                    return 0
                if retention_days < cache_retention_days:
                    logger.warning(
                        f"task_status_retention_days({retention_days}) 短于 "
                        f"cache_retention_days({cache_retention_days})，已钳制为后者："
                        "/view/{view_token} 链接依赖 task_status 行解析，"
                        "提前删除会造成缓存尚在、链接已死"
                    )
                    retention_days = cache_retention_days

            cutoff = (
                datetime.datetime.now(datetime.timezone.utc)
                - datetime.timedelta(days=retention_days)
            ).strftime('%Y-%m-%d %H:%M:%S')

            with self._get_cursor() as cursor:
                cursor.execute('''
                    DELETE FROM task_status
                    WHERE status IN (?, ?)
                      AND COALESCE(completed_at, created_at) < ?
                ''', (TaskStatus.SUCCESS, TaskStatus.FAILED, cutoff))

                deleted_count = cursor.rowcount

            logger.info(f"清理了 {deleted_count} 条超过 {retention_days} 天的终态任务状态记录")
            return deleted_count

        except Exception as e:
            logger.error(f"清理任务状态记录失败: {e}")
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
    
    def create_task(self, url: str, use_speaker_recognition: bool = False,
                    download_url: str = None, platform: str = None,
                    media_id: str = None) -> Dict[str, str]:
        """
        创建新任务，相同URL或相同(platform, media_id)会复用view_token

        查重策略（按优先级）：
        1. 精确 URL 匹配（现有行为）
        2. (platform, media_id) 语义匹配（新增，解决同一视频不同 URL 格式的问题）

        Args:
            url: 视频URL（平台链接，用于 view_token 和缓存）
            use_speaker_recognition: 是否使用说话人识别
            download_url: 实际下载地址（可选，如果提供则优先使用）
            platform: 平台名称（可选，由 URLParser 提取）
            media_id: 媒体ID（可选，由 URLParser 提取）

        Returns:
            Dict: 包含task_id和view_token的字典
        """
        task_id = self.generate_task_id()

        # 将空字符串转换为 None，避免存储无意义的空字符串
        if download_url is not None and not download_url.strip():
            download_url = None

        # 策略1: 精确 URL 匹配（现有行为）
        existing_task = self.get_existing_task_by_url(url, use_speaker_recognition)
        if existing_task:
            view_token = existing_task['view_token']
            logger.debug(f"通过URL精确匹配复用view_token: {view_token} (状态: {existing_task['status']}) for URL: {url}")
        else:
            # 策略2: (platform, media_id) 语义匹配（解决同源视频不同URL格式的问题）
            media_task = self.get_existing_task_by_media(platform, media_id, use_speaker_recognition)
            if media_task:
                view_token = media_task['view_token']
                logger.info(
                    f"通过平台+媒体ID语义匹配复用view_token: {view_token} "
                    f"(platform={platform}, media_id={media_id})"
                )
            else:
                view_token = self.generate_view_token()
                logger.info(f"生成新view_token: {view_token} for URL: {url}")

        try:
            with self._get_cursor() as cursor:
                cursor.execute('''
                    INSERT INTO task_status
                    (task_id, view_token, url, download_url, use_speaker_recognition,
                     status, platform, media_id)
                    VALUES (?, ?, ?, ?, ?, 'queued', ?, ?)
                ''', (task_id, view_token, url, download_url, use_speaker_recognition,
                      platform, media_id))

            logger.info(f"任务创建成功: {task_id}, view_token: {view_token}, download_url: {download_url}")
            return {
                "task_id": task_id,
                "view_token": view_token
            }
        except Exception as e:
            logger.error(f"创建任务失败: {e}")
            raise
    
    def update_task_status(self, task_id: str, status: str, platform: str = None,
                          media_id: str = None, title: str = None, author: str = None,
                          cache_id: int = None, download_url: str = None,
                          force: bool = False, error_message: str = None,
                          calibration_status: str = None, summary_status: str = None):
        """
        更新任务状态

        终态黏性:默认情况下,已处于终态(success/failed)的任务不会被覆写,
        防止慢半拍的旧 worker、重复任务或异常重试把已完成的任务覆回处理中。
        recalibrate 等需要显式重置的场景传 force=True 绕过该保护。

        Args:
            task_id: 任务ID
            status: 状态 (queued/processing/calibrating/success/failed)
            platform: 平台名称
            media_id: 媒体ID
            title: 视频标题
            author: 作者
            cache_id: 关联的缓存ID
            download_url: 实际下载地址
            force: 是否绕过终态黏性保护(recalibrate 显式重置时为 True)
            calibration_status: CalibrationStatus 取值(full/partial/none)，
                "诚实状态模型"落盘到 task_status 表，供 /api/audit/history 等查询消费。
                None 表示不更新该列。
            summary_status: SummaryStatus 取值(generated/skipped_short/failed/pending)，
                None 表示不更新该列。
        """
        try:
            # 将空字符串转换为 None，避免存储无意义的空字符串
            if download_url is not None and isinstance(download_url, str) and not download_url.strip():
                download_url = None

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
                if download_url is not None:  # 只有非空的 download_url 才更新
                    update_fields.append("download_url = ?")
                    params.append(download_url)
                if error_message is not None:
                    update_fields.append("error_message = ?")
                    params.append(error_message)
                if calibration_status is not None:
                    update_fields.append("calibration_status = ?")
                    params.append(calibration_status)
                if summary_status is not None:
                    update_fields.append("summary_status = ?")
                    params.append(summary_status)

                if status in ['success', 'failed']:
                    update_fields.append("completed_at = CURRENT_TIMESTAMP")

                params.append(task_id)

                # 终态黏性:非 force 时,已是 success/failed 的行不被覆写
                where = "task_id = ?"
                if not force:
                    where += " AND status NOT IN (?, ?)"
                    params.extend([TaskStatus.SUCCESS, TaskStatus.FAILED])

                query = f"UPDATE task_status SET {', '.join(update_fields)} WHERE {where}"
                cursor.execute(query, params)

                if cursor.rowcount == 0 and not force:
                    logger.info(
                        f"任务状态更新被终态黏性拦截(已是终态,跳过): {task_id} -> {status}"
                    )
                else:
                    logger.info(f"任务状态更新: {task_id} -> {status}")

        except Exception as e:
            logger.error(f"更新任务状态失败: {e}")
            raise

    def recover_orphaned_tasks(self) -> int:
        """启动恢复:将中断的非终态任务标记为 failed.

        任务队列存在内存中,进程崩溃/重启时正在处理的任务会随队列丢失,
        DB 里会永远停在 queued/processing/calibrating。启动时调用一次,
        把这些僵尸任务统一标为 failed,避免客户端白白轮询到超时。

        命中 idx_task_status 索引。标 failed 而非重新入队,避免重复下载/扣费。

        Returns:
            int: 被恢复(标记为 failed)的任务数
        """
        try:
            with self._get_cursor() as cursor:
                cursor.execute(
                    "UPDATE task_status SET status = ?, completed_at = CURRENT_TIMESTAMP "
                    "WHERE status IN (?, ?, ?)",
                    (
                        TaskStatus.FAILED,
                        TaskStatus.QUEUED,
                        TaskStatus.PROCESSING,
                        TaskStatus.CALIBRATING,
                    ),
                )
                count = cursor.rowcount
            if count:
                logger.warning(
                    f"启动恢复:将 {count} 个中断任务"
                    f"(queued/processing/calibrating)标记为 failed"
                )
            else:
                logger.info("启动恢复:无中断任务需要处理")
            return count
        except Exception as e:
            logger.error(f"启动恢复扫描失败: {e}")
            return 0

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
    
    def _resolve_summary_state(
        self, task_info: Dict[str, Any], cache_data: Dict[str, Any]
    ) -> tuple:
        """解析总结的展示状态与展示文本（"诚实状态模型"，修复"总结处理中..."永久占位符 bug）。

        来源优先级：task_status.summary_status 列 > llm_status.json 里的
        summary_status（两者本应一致，列是 JSON 的镜像，任一缺失时互为兜底）
        > 历史兼容推断（两者都没有的旧任务，按 llm_summary.txt 是否存在推断）。

        Args:
            task_info: get_task_by_view_token 返回的任务行（dict，含 summary_status 列）
            cache_data: get_cache 返回的缓存数据（含 llm_summary / llm_status 字段）

        Returns:
            (summary_state, summary_text) 元组：
            - summary_state: SummaryStatus 取值之一，前端据此渲染四种文案
            - summary_text: GENERATED 时为真实总结文本，其余状态一律为 None
              （不再返回"总结处理中..."之类的占位字符串，占位交给前端按 state 渲染）
        """
        summary_status = task_info.get('summary_status')
        if not summary_status:
            llm_status = cache_data.get('llm_status') or {}
            summary_status = llm_status.get('summary_status')

        raw_summary = cache_data.get('llm_summary')
        if raw_summary is not None and not isinstance(raw_summary, str):
            raw_summary = str(raw_summary)
        has_summary_text = bool(raw_summary)

        if summary_status:
            if summary_status == SummaryStatus.GENERATED:
                return SummaryStatus.GENERATED, (raw_summary if has_summary_text else None)
            # skipped_short / failed / pending：一律不展示占位文本，交给前端按状态渲染
            return summary_status, None

        # 历史兼容：既没有 task_status 列也没有 llm_status.json 的旧任务
        # （早于本功能上线）——按是否存在 llm_summary.txt 推断，
        # 无法进一步区分"文本过短"与"生成失败"，保守归入 skipped_short（非错误态）。
        if has_summary_text:
            return SummaryStatus.GENERATED, raw_summary
        return SummaryStatus.SKIPPED_SHORT, None

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

            display_url = task_info.get('url') or task_info.get('download_url') or ""

            # 如果任务还在进行中（calibrating 表示转录已完成、LLM 校对/总结进行中）
            if task_info['status'] in ['queued', 'processing', 'calibrating']:
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
                    'message': task_info.get('error_message') or '转录任务失败，请重新提交'
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
                    # summary_state：诚实状态模型，取代过去"文件缺失=处理中"的无条件占位符
                    # （旧 bug：cache_data.get('llm_summary', '总结处理中...') 把文件缺失、
                    # 总结过短跳过、总结生成失败三种完全不同的情况全部误判为"处理中"）
                    summary_state, summary = self._resolve_summary_state(task_info, cache_data)

                    transcript = cache_data.get('llm_calibrated') or cache_data.get('transcript_data', '转录文本获取中...')
                    if not isinstance(transcript, str):
                        transcript = str(transcript) if transcript is not None else '转录文本获取中...'

                    # 获取 LLM 模型配置（缓存命中任务无 llm_config，回退查同 view_token 下的历史任务）
                    llm_config = self.get_task_llm_config(task_info['task_id'])
                    if not llm_config:
                        llm_config = self._get_llm_config_by_view_token(
                            task_info['view_token']
                        )

                    return {
                        'status': 'success',
                        'title': cache_data.get('title', ''),
                        'author': cache_data.get('author', ''),
                        'description': cache_data.get('description', ''),
                        'url': display_url,
                        'summary': summary,
                        'summary_state': summary_state,
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
    
    def get_cache_by_view_token(self, view_token: str) -> Optional[Dict[str, Any]]:
        """根据 view_token 获取完整缓存数据（含转录文件路径和元数据）

        从 task_status 表找到 url、platform、media_id、use_speaker_recognition，
        再从 video_cache 表获取完整缓存数据。

        Args:
            view_token: 查看 token

        Returns:
            Dict: 完整缓存数据（与 get_cache 返回格式一致），包含额外的 task_info 字段；
                  如果未找到则返回 None
        """
        try:
            # 获取任务信息
            task_info = self.get_task_by_view_token(view_token)
            if not task_info:
                logger.warning(f"未找到 view_token 对应的任务: {view_token}")
                return None

            platform = task_info.get("platform")
            media_id = task_info.get("media_id")
            use_speaker_recognition = task_info.get("use_speaker_recognition", False)

            if not platform or not media_id:
                logger.warning(
                    f"任务信息不完整，缺少 platform 或 media_id: "
                    f"view_token={view_token}, platform={platform}, media_id={media_id}"
                )
                return None

            # 获取缓存数据
            cache_data = self.get_cache(
                platform=platform,
                media_id=media_id,
                use_speaker_recognition=use_speaker_recognition,
            )

            if cache_data:
                # 附加任务信息，方便调用方使用
                cache_data["task_info"] = task_info
                logger.info(
                    f"通过 view_token 获取缓存成功: platform={platform}, media_id={media_id}"
                )

            return cache_data

        except Exception as e:
            logger.error(f"通过 view_token 获取缓存失败: {e}")
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
                    SELECT task_id, view_token, url, download_url, use_speaker_recognition, status,
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
                        'download_url': row[3],
                        'use_speaker_recognition': bool(row[4]),
                        'status': row[5],
                        'title': row[6],
                        'author': row[7],
                        'platform': row[8],
                        'media_id': row[9],
                        'cache_id': row[10],
                        'created_at': row[11]
                    }
                    logger.debug(f"找到现有任务: {task_info['task_id']}, 状态: {task_info['status']}, URL: {url}")
                    return task_info
                else:
                    logger.debug(f"未找到现有任务: URL={url}, use_speaker_recognition={use_speaker_recognition}")
                    return None

        except Exception as e:
            logger.error(f"查找现有任务失败: {e}")
            return None

    def get_existing_task_by_media(self, platform: str, media_id: str,
                                   use_speaker_recognition: bool = False) -> Optional[Dict[str, Any]]:
        """
        根据(platform, media_id)查找现有任务，用于同源视频不同URL格式的去重

        Args:
            platform: 平台名称
            media_id: 媒体ID
            use_speaker_recognition: 是否使用说话人识别

        Returns:
            Optional[Dict]: 现有任务信息，如果没有找到则返回None
        """
        # 参数校验：platform 或 media_id 为 None 时跳过查询
        if not platform or not media_id:
            return None

        try:
            with self._get_cursor() as cursor:
                cursor.execute('''
                    SELECT task_id, view_token, url, download_url, use_speaker_recognition, status,
                           title, author, platform, media_id, cache_id, created_at
                    FROM task_status
                    WHERE platform = ? AND media_id = ? AND use_speaker_recognition = ?
                    ORDER BY
                        CASE status
                            WHEN 'success' THEN 1
                            WHEN 'processing' THEN 2
                            WHEN 'queued' THEN 3
                            ELSE 4
                        END,
                        created_at DESC
                    LIMIT 1
                ''', (platform, media_id, use_speaker_recognition))

                row = cursor.fetchone()
                if row:
                    task_info = {
                        'task_id': row[0],
                        'view_token': row[1],
                        'url': row[2],
                        'download_url': row[3],
                        'use_speaker_recognition': bool(row[4]),
                        'status': row[5],
                        'title': row[6],
                        'author': row[7],
                        'platform': row[8],
                        'media_id': row[9],
                        'cache_id': row[10],
                        'created_at': row[11]
                    }
                    logger.debug(
                        f"通过平台+媒体ID找到现有任务: {task_info['task_id']}, "
                        f"状态: {task_info['status']}, platform={platform}, media_id={media_id}"
                    )
                    return task_info
                else:
                    logger.debug(
                        f"未通过平台+媒体ID找到现有任务: platform={platform}, media_id={media_id}"
                    )
                    return None

        except Exception as e:
            logger.error(f"通过平台+媒体ID查找现有任务失败: {e}")
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

    def _get_llm_config_by_view_token(self, view_token: str) -> Optional[Dict[str, Any]]:
        """回退查找：在同一 view_token 的所有任务中，找最新的 llm_config。

        缓存命中的任务不经过 LLM 协调器，没有 llm_config。
        此方法用于从同一 view_token 下实际跑过 LLM 的历史任务继承配置。
        """
        try:
            with self._get_cursor() as cursor:
                cursor.execute(
                    """SELECT llm_config FROM task_status
                       WHERE view_token = ?
                         AND llm_config IS NOT NULL
                         AND llm_config != ''
                       ORDER BY created_at DESC
                       LIMIT 1""",
                    (view_token,),
                )
                row = cursor.fetchone()
                if row and row['llm_config']:
                    return json.loads(row['llm_config'])
                return None
        except Exception as e:
            logger.error(f"回退查找LLM配置失败: {e}")
            return None
