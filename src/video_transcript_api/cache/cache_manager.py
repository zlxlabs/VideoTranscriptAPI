import os
import json
import sqlite3
import datetime
import uuid
import secrets
from pathlib import Path
from typing import Optional, Dict, Any, List, Tuple, Union
from contextlib import contextmanager
import threading
import time
from ..utils.logging import setup_logger
from ..utils.task_status import TaskStatus
from ..utils.llm_status import SummaryStatus

logger = setup_logger("cache_manager")


# 运行期对账宽限期（本地 codex review 第 12 轮 P1 发现 c）：进程内在途
# 任务登记表（RuntimeContext.inflight_registry）是主要保护——只要任务
# 仍在登记表里，不论运行多久都不会被 CacheManager.
# reconcile_runtime_orphaned_tasks 误杀，这是比下面的时间阈值更强的
# 保护。时间宽限只是第二道保险，专门兜住"队列拒绝后补写 failed 本身
# 也失败"这类登记表覆盖不到的缝隙（见 api/routes/tasks.py 两处 503
# 分支里 update_task_status 失败时的兜底日志）——这次清理写入本身失败
# 后，任务行会永久停留在非终态，且不属于 aclose() 关闭清算或启动孤儿
# 恢复覆盖的任一触发时机（两者只在重启/关闭时点各触发一次，服务持续
# 运行期间无人再看这行）。
#
# 取值依据：单任务全流程最坏情况下依次经过的各阶段超时之和——download
# （storage.timeout=300s）+ FunASR 说话人识别 ASR 阶段
# （funasr_spk_server.total_timeout=3600s，"跨所有 phase/retry 的硬
# 上限"，见 config/config.example.jsonc）+ LLM 校对/总结/说话人推断
# 三个阶段（各自 total_timeout=300s）≈ 4800s（80 分钟）。取整到
# 5400s（90 分钟）留出余量，宽限期取其 2 倍。
MAX_EXPECTED_TASK_DURATION_SECONDS = 5400
RUNTIME_RECONCILE_GRACE_SECONDS = 2 * MAX_EXPECTED_TASK_DURATION_SECONDS

# 清算路径 SQLite 连接的默认 busy_timeout（毫秒，本地 codex review 第
# 12 轮 P2 发现 e）：匹配 CacheManager._get_connection() 里 sqlite3.
# connect() 沿用的模块默认值 timeout=5.0（未显式传入 timeout 参数）。
# _fail_non_terminal_tasks 的清算专属 busy_timeout 收窄结束后，用这个
# 值把当前线程连接的 busy_timeout 还原，避免这个连接后续被同一线程挪
# 作他用（如该线程恰好也是某个 asyncio.to_thread 调用复用的默认执行
# 器线程）时继续沿用一份被收紧过的短 busy_timeout。
_DEFAULT_SQLITE_BUSY_TIMEOUT_MS = 5000

# 清算路径 busy_timeout 下限（毫秒，本地 codex review 第 12 轮 P2 发现
# e）：剩余预算换算成毫秒后可能只有个位数甚至 0——此时任何锁竞争都会让
# SQLite 立即返回 SQLITE_BUSY，即便真实持锁时间只需要再等几毫秒就会
# 释放。用一个小的下限换取更高的成功率，代价是最坏情况下单次调用可能
# 比"严格剩余预算"多阻塞这个下限的量——量级是毫秒，不会明显破坏
# aclose() 秒级的整体有界性承诺。
_SHUTDOWN_DRAIN_MIN_BUSY_TIMEOUT_MS = 50

# 周期清理（cleanup_old_cache）逐条抢占媒体锁的最长等待秒数（U1，PR3 review
# hardening）：写者（llm_ops._save_llm_results / CacheManager.save_llm_status）
# 持有 media_lock 的范围只覆盖磁盘 I/O 本身（判断分层缓存是否已存在 -> 写文件
# -> 合并落盘 llm_status.json），不包含耗时的 LLM API 调用（那部分发生在拿锁
# 之前）——量级是毫秒到低个位数秒。取与 _DEFAULT_SQLITE_BUSY_TIMEOUT_MS 相同
# 量级的 5 秒上限：真等满说明存在异常阻塞，跳过这一条留给下一轮周期清理重试，
# 避免单条记录卡死整个清理任务（媒体锁不可用时的降级策略见 cleanup_old_cache）。
_CLEANUP_MEDIA_LOCK_TIMEOUT_SECONDS = 5.0

class CacheManager:
    """
    管理视频转录缓存的类
    使用 SQLite 数据库存储元数据，文件系统存储实际内容
    """

    # 从多条同源 task_status 记录中挑选"最合适"一条时共用的 ORDER BY 排序表达式。
    # 排序策略分三段，不穷举具体状态值，避免状态机演进（新增状态）时腐化：
    # - success 永远最高优先级（优先返回成功完成的任务）
    # - failed 永远垫底，避免旧的失败记录掩盖同一 key（view_token / url /
    #   (platform, media_id)）下更新的、仍在处理中（queued/processing/
    #   calibrating 等）或已成功的任务
    # - 其余任何状态（不管是当前已知的还是未来新增的）统一排在中间优先级，
    #   组内按 created_at DESC 排序，返回最新的一条
    # 被 get_task_by_view_token / get_existing_task_by_url /
    # get_existing_task_by_media 三处共用：这段排序逻辑已经因为各自维护
    # 独立字面量 SQL 而漏改过两次（遗漏 calibrating 分支），提取成单一
    # 常量后，状态机再演进只需改这一处。
    _TASK_STATUS_PRIORITY_ORDER_BY = (
        "CASE WHEN status = 'success' THEN 0 "
        "WHEN status = 'failed' THEN 2 "
        "ELSE 1 END, created_at DESC"
    )

    # Z3（PR3 review hardening 本轮）：save_cache 在“建新行”场景（该
    # (platform, media_id) 当前完全没有任何存活的 video_cache 行）写入前
    # 需要清理的已知产物文件名清单——Y5 把 rmtree 失败留下的目录当无害
    # 孤儿，但目录路径由 platform/年月/media_id 确定性生成，会被后续同一
    # (platform, media_id) 的请求原样复用；若不清理，残留的旧格式转录/
    # LLM/说话人产物会被新行意外读到，新旧内容混用。与各写侧方法（
    # save_cache 自身 / save_llm_status / save_speaker_mapping）实际落盘
    # 的文件名字面量保持一致——做减法，不新增独立的命名常量模块。
    _KNOWN_ARTIFACT_FILENAMES = (
        "transcript_funasr.json",
        "transcript_capswriter.txt",
        "transcript_capswriter.json",
        "llm_calibrated.txt",
        "llm_summary.txt",
        "llm_status.json",
        "llm_processed.json",
        "speaker_mapping.json",
    )

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
        self.audit_logger = None

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
        self._terminal_archive_lock = threading.RLock()

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

    def terminal_archive_lock(self):
        """Serialize audit repair with expire-and-delete cleanup transitions."""
        return self._terminal_archive_lock
        
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

    def _apply_connection_busy_timeout_ms(self, timeout_ms: int) -> None:
        """在当前线程的连接上设置 busy_timeout（毫秒，本地 codex review 第
        12 轮 P2 发现 e）。

        只在清算路径（_fail_non_terminal_tasks 收到显式 deadline_seconds
        时）调用，把 SQLite 锁竞争的最大等待时间收窄到调用方实际剩余的
        关闭预算，取代连接默认继承自 sqlite3.connect() 的 timeout=5.0（见
        _DEFAULT_SQLITE_BUSY_TIMEOUT_MS 的说明）——否则单次锁竞争最坏可以
        真的等满 5s，与调用方声明的（通常小得多的）deadline_seconds 脱节。

        PRAGMA busy_timeout 不支持 sqlite3 模块的 `?` 参数绑定占位符
        （PRAGMA 语句本身的语法限制，绑定参数会报语法错误），这里直接拼
        接整数字面量——timeout_ms 永远来自内部计算（剩余关闭预算换算、
        或还原用的默认值常量），不接受外部输入，不存在注入风险。直接在
        connection 上 execute（不经过 _get_cursor()）：PRAGMA 不是需要
        commit/rollback 包裹的普通数据修改语句，没必要套用那层事务化的
        上下文管理器。
        """
        timeout_ms = max(0, int(timeout_ms))
        self._get_connection().execute(f"PRAGMA busy_timeout = {timeout_ms}")

    def _shutdown_drain_busy_timeout_ms(self, deadline: float) -> int:
        """把 deadline（monotonic 绝对时间）换算成"剩余预算，钳制到下限"
        的 busy_timeout 毫秒数，供 _apply_connection_busy_timeout_ms 使用
        （本地 codex review 第 12 轮 P2 发现 e，下限依据见
        _SHUTDOWN_DRAIN_MIN_BUSY_TIMEOUT_MS 上方的说明）。"""
        remaining_seconds = max(0.0, deadline - time.monotonic())
        return max(
            _SHUTDOWN_DRAIN_MIN_BUSY_TIMEOUT_MS, int(remaining_seconds * 1000)
        )

            
    @contextmanager
    def media_lock(self, platform: str, media_id: str, timeout: Optional[float] = None):
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

        Args:
            timeout: 拿锁的最长等待秒数（U1，PR3 review hardening 新增）。
                None（默认）沿用原有行为——无限阻塞直到拿到锁，是当前全部既有
                调用方（llm_ops._save_llm_results、save_llm_status 等）的既有
                语义，不受影响。传入非 None 值且超时未拿到锁时抛出
                TimeoutError，调用方可捕获后放弃本次操作（例如
                cleanup_old_cache 跳过这条记录，留给下一轮周期清理重试）。
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
        # acquire(timeout=None) 等价于 acquire()（无限阻塞），与原有行为完全
        # 一致；acquire(timeout=非负数) 是 threading.RLock 的标准接口，超时
        # 未拿到锁返回 False，不抛异常。
        acquired = lock.acquire(timeout=timeout if timeout is not None else -1)
        if not acquired:
            # 拿锁失败：上面已经把引用计数 +1（严格早于 acquire()，见上方
            # 计数窗口的说明），这里必须原样回退，否则这把锁永远无法从字典
            # 弹出，等价于每次超时都泄漏一个锁池条目。
            with self._media_locks_guard:
                self._media_lock_refcounts[key] -= 1
                if self._media_lock_refcounts[key] <= 0:
                    self._media_locks.pop(key, None)
                    self._media_lock_refcounts.pop(key, None)
            raise TimeoutError(
                f"media_lock timeout after {timeout}s waiting for {key}"
            )
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
                    processing_options TEXT,
                    submitted_by TEXT,
                    terminal_snapshot TEXT,
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

                # 迁移6: 每次请求自己的规范化选项、提交者与不可变终态快照。
                cursor.execute("PRAGMA table_info(task_status)")
                columns = [col[1] for col in cursor.fetchall()]
                for column in ("processing_options", "submitted_by", "terminal_snapshot"):
                    if column not in columns:
                        cursor.execute(f"ALTER TABLE task_status ADD COLUMN {column} TEXT")

                if all(c in columns for c in ('calibration_status', 'summary_status', 'error_message')):
                    logger.debug("数据库结构正常，无需迁移")

        except Exception as e:
            logger.error(f"数据库迁移失败: {e}")
            raise

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
                processing_options TEXT,
                submitted_by TEXT,
                terminal_snapshot TEXT,
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
                row_map.get("processing_options"),
                row_map.get("submitted_by"),
                row_map.get("terminal_snapshot"),
            ]

            cursor.execute('''
                INSERT INTO task_status
                (task_id, view_token, url, download_url, platform, media_id, use_speaker_recognition,
                 status, title, author, created_at, completed_at, cache_id, llm_config,
                 error_message, calibration_status, summary_status, processing_options,
                 submitted_by, terminal_snapshot)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            # Y6 修复（PR3 review hardening 加固轮，核实结论见评审复盘文档）：
            # 文件写入 + DB 落库整体纳入该媒体的 media_lock 临界区，与
            # save_speaker_mapping/save_llm_status/_save_llm_results 等既有
            # 写路径用同一把锁（U1，做减法，不引入新机制）。
            #
            # 核实结论：评审称"U1 清理复核前提失效"——复核依据 U1 的锁内
            # 双复核之一（"该媒体存在非终态 task_status 行则跳过"）覆盖了
            # save_cache 的写入窗口，但 save_cache 本身此前完全不持锁。
            # 核实 save_cache 唯一的调用链（transcription.py::
            # process_transcription，6 个调用点）：process_task_queue 把
            # 任务提交给 worker 之前，已经把该 task_id 的 task_status 行
            # 写成 PROCESSING（非终态），且该行在这次 save_cache 调用返回
            # 之前始终保持非终态（写 CALIBRATING/FAILED 都严格发生在
            # save_cache 调用之后）——复核 1 因此天然覆盖"当前任务自己的
            # save_cache 写入"这个具体窗口，评审对这一点的复核前提判断
            # 没有错，但同一次核实过程中发现一个相邻的真实缺口：
            # cleanup_old_cache 对每条候选记录的"复核 -> 删除"判定只发生在
            # 拿到锁的那一刻，如果判定当时这个媒体压根没有任何任务（两次
            # 复核均合法通过），随后它仍持锁执行 rmtree（目录较大或磁盘
            # 较慢时可能有明显耗时）期间，一个全新请求恰好为同一媒体重新
            # 落地（比如很久没人看的旧内容与它自己的清理周期撞在一起）——
            # 该请求的 save_cache 此前完全不检查 media_lock，会在 cleanup
            # 的 rmtree 进行中并发 mkdir/写入同一目录，物理竞争新产物与
            # 正在被拆除的旧目录。补持同一把锁后，save_cache 会在 cleanup
            # 释放锁之前排队等待；锁释放时 cleanup 的删除已完全落定（DB 行
            # 也已先于 rmtree 删除，见 cleanup_old_cache 的 Y5 修复），
            # save_cache 随后拿到锁时面对的是一个 DB 中已无引用的旧目录，
            # mkdir 重新创建、正常写入，不再有并发窗口。
            with self.media_lock(platform, media_id):
                # 获取文件存储路径
                file_path = self._get_file_path(platform, media_id)

                # Z3（PR3 review hardening 本轮）：写入前清理 rmtree 失败
                # 残留的孤儿产物文件——仅当 (platform, media_id) 当前完全
                # 没有任何存活的 video_cache 行（任一 use_speaker_
                # recognition 变体）时才清理。Y5 修复已经保证 DB 行严格
                # 先于 rmtree 删除，所以“目录存在但没有任何行引用它”只
                # 可能是 rmtree 失败的孤儿残留，不会是别的情况；只要还有
                # 一行存活（本变体的正常覆盖写，或 W3 场景下另一变体仍
                # 引用同一目录），目录里的文件就都是仍在被引用的有效
                # 产物，不能碰——跳过清理。仍在同一把 media_lock 临界区
                # 内做这次判定 + 清理 + 写入，避免跟另一变体的并发写产生
                # 新的竞争窗口。
                with self._get_cursor() as cursor:
                    cursor.execute(
                        "SELECT 1 FROM video_cache WHERE platform = ? AND media_id = ? LIMIT 1",
                        (platform, media_id),
                    )
                    has_any_live_row = cursor.fetchone() is not None
                if not has_any_live_row and file_path.exists():
                    orphan_cleanup_failed_filenames = (
                        self._cleanup_orphaned_artifact_files(file_path)
                    )
                    if orphan_cleanup_failed_filenames:
                        # K2（CI review 第 3 轮 major）：删除失败的残留
                        # 文件若不会被本轮写入覆盖，会被 get_cache 按既有
                        # 读取优先级（transcript_funasr.json 优先于
                        # capswriter 系；llm_calibrated.txt/llm_summary.txt/
                        # llm_status.json/llm_processed.json/
                        # speaker_mapping.json 只要存在就会被无条件读取）
                        # 连带读出——新 video_cache 行因此会让调用方读到
                        # 新旧混合的产物，不能提交新行、也不能返回成功
                        # 掩盖这个真实冲突。本轮实际会写入哪些产物文件名
                        # 取决于 transcript_type/extra_json_data，据此算出
                        # "本轮会覆盖"的文件名集合——只有落在这个集合之外
                        # 的删除失败才算真正冲突（本轮会重写的同名文件，
                        # 删除是否成功不影响最终内容，不算冲突）。
                        overwritten_this_round = {
                            "transcript_funasr.json" if transcript_type == "funasr"
                            else "transcript_capswriter.txt"
                        }
                        if transcript_type != "funasr" and extra_json_data:
                            overwritten_this_round.add("transcript_capswriter.json")
                        conflicting_failures = (
                            orphan_cleanup_failed_filenames - overwritten_this_round
                        )
                        if conflicting_failures:
                            logger.error(
                                f"孤儿残留产物清理失败且与本轮写入冲突，"
                                f"放弃本次保存: {platform}/{media_id}, "
                                f"冲突文件: {sorted(conflicting_failures)}"
                            )
                            return None

                file_path.mkdir(parents=True, exist_ok=True)

                # 保存转录文件
                if transcript_type == "funasr":
                    transcript_file = file_path / "transcript_funasr.json"
                    # 原子写（G5）：json.dump 直接写进 temp 文件对象，序列化
                    # 中途抛错也不会影响 transcript_file 里已有的旧转录。
                    self._atomic_write(
                        transcript_file,
                        lambda f: json.dump(transcript_data, f, ensure_ascii=False, indent=2),
                    )
                else:
                    transcript_file = file_path / "transcript_capswriter.txt"

                    # 覆盖写不再携带 timeline 时，必须在写入新正文之前清掉
                    # 上一轮可能残留的旧侧车 transcript_capswriter.json：
                    # 本目录是 (platform, media_id) 的确定性路径，上一轮的
                    # 侧车在本轮不会被重写（下面的 if 只在 extra_json_data
                    # 为真时才写），而 Z3 的孤儿清理只在"没有任何存活
                    # video_cache 行"时才触发，正常覆盖写（行仍存在）走不到
                    # 那里——不主动删除的话，get_cache 会经 load_segments 把
                    # 上一轮旧 segments 读回来，与本轮新正文混用。删除放在
                    # 写新正文之前、失败时如实中止本次保存（与 K2 同一原则：
                    # 宁肯不写入新行，也不制造新旧产物混用的读取状态），这样
                    # 删除失败时磁盘上仍是上一轮的完整旧状态。
                    if not extra_json_data:
                        stale_json_file = file_path / "transcript_capswriter.json"
                        try:
                            stale_json_file.unlink(missing_ok=True)
                        except OSError as unlink_exc:
                            logger.error(
                                f"删除旧 timeline 侧车失败，放弃本次保存以避免"
                                f"新旧 segments/正文混用: {stale_json_file}: {unlink_exc}"
                            )
                            return None

                    self._atomic_write(transcript_file, lambda f: f.write(transcript_data))

                    # 如果提供了额外的JSON数据（CapsWriter的FunASR兼容格式），也保存
                    if extra_json_data:
                        json_file = file_path / "transcript_capswriter.json"
                        self._atomic_write(
                            json_file,
                            lambda f: json.dump(extra_json_data, f, ensure_ascii=False, indent=2),
                        )
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

    def _cleanup_orphaned_artifact_files(self, file_path: Path) -> set:
        """删除 rmtree 失败残留目录里的已知产物文件（Z3，PR3 review
        hardening 本轮；K2，CI review 第 3 轮 major 补删除失败的冲突
        判定）。

        只应在调用方已经确认该 (platform, media_id) 当前没有任何存活的
        video_cache 行时调用（见 save_cache 的调用处判断）——这是 Y5
        修复后“目录存在但无行引用”唯一可能对应的场景：cleanup_old_cache
        严格先删 DB 行、后 rmtree，若 rmtree 失败，目录连同其中的旧产物
        原样残留，但确定性路径（platform/年月/media_id）会被同一
        (platform, media_id) 的后续请求复用，不清理就会造成新旧产物混用
        （get_cache 按固定优先级 transcript_funasr.json > transcript_
        capswriter.txt 读取，旧格式文件残留会持续掩盖新格式文件）。

        逐个 unlink 已知文件名（_KNOWN_ARTIFACT_FILENAMES），不整目录
        rmtree：目录本身接下来会被 save_cache 重新写入，没必要删除后
        再 mkdir；且 unlink 只影响已知的产物文件名，不会误删目录里可能
        存在的、与这份清单无关的其它文件。

        返回值变更（K2）：此前本方法返回 None，删除失败只记 warning、
        调用方无从得知具体哪些文件没删掉，只能把这次清理当成"尽力而为、
        不影响写入结果"。save_cache 现在需要这份信息做冲突判定——残留
        文件若恰好是 get_cache 读取时优先选中、或本轮不会覆盖的产物，
        继续写入新 video_cache 行会让读取器读到新旧混合的内容（如残留的
        旧 transcript_funasr.json 优先于本轮新写入的
        transcript_capswriter.txt 被读取器选中）。改为返回删除失败的
        文件名集合，调用方据此判断是否需要如实中止本次保存，不再是纯粹
        的尽力而为。

        Args:
            file_path: 目标目录（_get_file_path 算出的确定性路径）。

        Returns:
            set[str]: 删除失败的文件名集合（相对 file_path 的 basename，
            取自 _KNOWN_ARTIFACT_FILENAMES）；全部成功（或文件本就不
            存在）时为空集合。
        """
        failed_filenames = set()
        for filename in self._KNOWN_ARTIFACT_FILENAMES:
            stale_file = file_path / filename
            try:
                stale_file.unlink(missing_ok=True)
            except OSError as unlink_exc:
                logger.warning(
                    f"清理孤儿目录残留产物失败: "
                    f"{stale_file}: {unlink_exc}"
                )
                failed_filenames.add(filename)
        logger.info(
            f"孤儿目录残留产物清理完成: {file_path}，删除失败: "
            f"{failed_filenames or '无'}"
        )
        return failed_filenames

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

                # Timeline segments 侧车：经统一适配器 load_segments 读回
                # transcript_funasr.json / transcript_capswriter.json。权威读法
                # 仍是 load_segments(cache_dir)；这里附带是为方便调用方。缺
                # 失或坏数据时诚实降级（不塞字段），不阻断缓存命中。
                try:
                    from video_transcript_api.transcriber.segments import load_segments

                    segments = load_segments(file_path)
                    if segments is not None:
                        cache_data["segments"] = segments
                except Exception as segments_exc:
                    # load_segments 自身契约是"不抛异常"；这里再兜一层，避免
                    # 未来改动破坏 get_cache 主路径。
                    logger.warning(f"读取 timeline segments 失败，忽略: {segments_exc}")

                cache_data['file_path'] = str(file_path)
                
                logger.info(f"缓存命中: {platform}/{media_id}, 说话人识别: {cache_data['use_speaker_recognition']}")
                return cache_data
                
        except Exception as e:
            logger.error(f"获取缓存失败: {e}")
            return None

    def _speaker_artifact_dir(self, platform: str, media_id: str) -> Optional[Path]:
        """Resolve the canonical artifact directory from video_cache.files_loc."""
        with self._get_cursor() as cursor:
            cursor.execute(
                "SELECT files_loc FROM video_cache "
                "WHERE platform=? AND media_id=? "
                "ORDER BY use_speaker_recognition DESC, updated_at DESC LIMIT 1",
                (platform, media_id),
            )
            row = cursor.fetchone()
        return self.cache_dir / Path(row[0]) if row else None

    def _resolve_variant_artifact_dir(
        self, platform: str, media_id: str, use_speaker_recognition: bool
    ) -> Optional[Path]:
        """按 get_cache()/save_llm_result()/save_llm_status() 同款row筛选口径
        定位缓存目录，只查数据库不读任何产物文件。

        V1 修复（PR3 review hardening）：_speaker_artifact_dir() 完全不看
        use_speaker_recognition，固定按 use_speaker_recognition DESC 排序
        优先命中说话人识别变体——同一 platform/media_id 下同时存在"说话人
        识别"与"普通"两个缓存变体时，任何经它定位目录的写侧操作都可能
        悄悄作用在错误的变体上。invalidate_llm_status() 正是因此撤销了
        与本轮实际重写无关的那个变体的状态，本轮真正重写
        的变体的旧状态反而原样残留，S1 的 write-ahead 撤销保护对错了
        目标（本次修复的直接起因）。

        与 get_cache() 的 platform+media_id 查询分支保持同一套筛选公式——
        use_speaker_recognition=True 时严格过滤到该变体；False 时不额外
        过滤，只按 DESC 排序把说话人识别变体放在前面（沿用 get_cache()
        "不要求说话人识别时，退而求其次也接受说话人识别变体"的既有语义，
        保证与 save_llm_result/save_llm_status 实际落盘的目录完全一致）。
        不直接复用 get_cache() 本身：那个方法还会读取转录/校对/总结等
        产物文件内容，对只需要一个目录路径的调用方来说是不必要的 I/O。

        Args:
            platform: 平台名称
            media_id: 媒体ID
            use_speaker_recognition: 是否精确匹配说话人识别变体

        Returns:
            Optional[Path]: 命中的缓存目录；无匹配行时返回 None。
        """
        with self._get_cursor() as cursor:
            conditions = ["platform = ? AND media_id = ?"]
            params: List[Any] = [platform, media_id]
            if use_speaker_recognition is True:
                conditions.append("use_speaker_recognition = 1")
            query = (
                f"SELECT files_loc FROM video_cache WHERE {' AND '.join(conditions)} "
                "ORDER BY use_speaker_recognition DESC, updated_at DESC LIMIT 1"
            )
            cursor.execute(query, params)
            row = cursor.fetchone()
        return self.cache_dir / Path(row[0]) if row else None

    @staticmethod
    def _speaker_mapping_result_is_valid(
        result: Dict[str, Any], speakers: List[str]
    ) -> bool:
        """校验说话人映射产物 result 的深层形状——读写两侧共用的唯一权威
        实现（R5，PR3 review hardening）。

        result 形状：{"mapping": {label: 展示名}, "meta": {label: {"name",
        "confidence", ...}}, "low_confidence": [label, ...]}（后者可选）。

        背景（本地 codex review 第 5 轮 F5，原本只内联在 get_speaker_
        mapping 读侧）：合法 JSON 但深层坏数据（meta[speaker] 不是 dict、
        缺 name/confidence、low_confidence 不可迭代）在消费点会抛异常：
        SpeakerInferencer.infer() 的缓存命中路径直接做
        meta[speaker]["name"]/["confidence"] 取值——TypeError/KeyError；
        _normalize_cached_result 对 low_confidence 做 list(...) 转换——
        TypeError。两处都没有各自的 try/except，会把"这份历史产物碰巧坏
        了"变成整个任务失败。

        R5 新增动机：save_speaker_mapping（写侧）此前完全不做这层校验，
        只验 source 字段，能把这里会拒绝的畸形内容（非 str 姓名/bool
        confidence，例如 LLM 返回的 JSON 里某个说话人的姓名字段是数字）
        写进 speaker_mapping.json——下一次请求的 get_speaker_mapping()
        读到后按缓存未命中处理，被迫重新烧一次 LLM；非字符串的展示名还会
        一路传导到 dialog_renderer，html.escape() 在非 str 输入上直接
        抛异常。现在读写两侧共用这同一个函数，写入前就拒绝，不再有"写得
        进、读不出"的产物，也不允许出现第二套校验逻辑。

        Returns:
            bool: True 表示形状合法，可以安全持久化/消费。
        """
        mapping = result.get("mapping")
        meta = result.get("meta")
        if not isinstance(mapping, dict) or not isinstance(meta, dict):
            return False
        expected_speakers = set(speakers)
        if not expected_speakers.issubset(mapping) or not expected_speakers.issubset(meta):
            return False

        for speaker in expected_speakers:
            # Y3 修复（PR3 review hardening 加固轮）：校验 mapping[speaker]
            # （展示名）本身是非空字符串。此前这个函数只查了 mapping 的 key
            # 集合覆盖 expected_speakers（上面的 issubset 检查），从未看过
            # mapping[speaker] 的值本身是什么类型——meta[speaker]["name"]
            # 是 str 不代表 mapping[speaker] 也是 str，两者是互相独立落盘的
            # 字段，畸形产物完全可能只坏了其中一个（比如 LLM 返回的展示名
            # 字段恰好是数字/None/list，但 meta.name 恰好是合法字符串）。
            # mapping[speaker] 最终会被 dialog_renderer 直接拼进 HTML
            # （html.escape() 要求 str 输入）、以及 llm_ops.py 的
            # _replace_speaker_labels_in_text 当作替换串使用——非 str 值不
            # 会在写入时被发现，而是一路存活到某次渲染/替换才炸 TypeError，
            # 把"这份历史产物碰巧坏了"变成整个任务失败，与本函数其它校验项
            # 要堵住的问题同一类。空字符串同样拒绝：空展示名不是有意义的
            # 说话人称呼，写入侧应当直接拒绝而不是留给渲染层静默显示空白。
            display_name = mapping.get(speaker)
            if not isinstance(display_name, str) or not display_name:
                return False

            entry = meta.get(speaker)
            if not isinstance(entry, dict):
                return False
            if not isinstance(entry.get("name"), str):
                return False
            confidence = entry.get("confidence")
            # bool 是 int 子类，isinstance(True, int) 为 True，但
            # True/False 不是有意义的置信度取值，显式排除。
            if not isinstance(confidence, (int, float)) or isinstance(confidence, bool):
                return False

        low_confidence = result.get("low_confidence")
        if low_confidence is not None:
            # 只接受 list/tuple/set：字符串本身也是可迭代对象（逐字符），
            # 若不排除，形如 "Speaker1" 这种被错误写成裸字符串的畸形值会
            # 被 all(isinstance(c, str) for c in "Speaker1") 误判为"合法
            # 的字符串可迭代对象"而放过。
            if not isinstance(low_confidence, (list, tuple, set)) or not all(
                isinstance(item, str) for item in low_confidence
            ):
                return False

        return True

    def get_speaker_mapping(
        self,
        platform: str,
        media_id: str,
        *,
        input_fingerprint: str,
        speakers: List[str],
    ) -> Optional[Dict[str, Any]]:
        """Load a complete, current speaker artifact from canonical media storage.

        损坏/不完整/形状不符的 artifact 一律视为 cache miss（返回 None），
        绝不让异常传播给调用方（本地 Codex review 发现的缺口）：
        SpeakerInferencer.infer() 对这个方法的调用没有包 try/except，任何
        未捕获的异常都会直接冒泡穿透 speaker-aware 处理管线，把"这份历史
        产物碰巧坏了"变成整个任务失败——而设计意图是自愈：读不到就当没有，
        让上游走通用标签或重新推断即可。

        此前只在 JSON 解析层（read_text/json.loads）兜底，下面的形状校验
        （尤其 `set(payload.get("speakers") or [])`）在 speakers 字段被存成
        非可迭代的标量（如整数/布尔，JSON 里合法但语义上是脏数据）时会直接
        抛 TypeError，未被捕获。这里把 JSON 解析之后、直到函数返回之前的
        全部校验逻辑一并纳入 try/except，捕获同一大类"形状不符"异常
        （TypeError/AttributeError），与已有的 OSError/JSONDecodeError
        （文件系统/语法层面的损坏）合并处理，行为始终是记录一条 warning
        并返回 None，不改变任何"合法但过期/不匹配"分支原有的 None 语义。

        深层形状校验（meta 每个条目的内部形状、low_confidence 可迭代性等）
        委托给 _speaker_mapping_result_is_valid——与 save_speaker_mapping
        （写侧）共用同一份实现（R5），不在这里重复维护第二份判断逻辑。
        """
        artifact_dir = self._speaker_artifact_dir(platform, media_id)
        if artifact_dir is None:
            return None
        path = artifact_dir / "speaker_mapping.json"
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                return None
            if (
                payload.get("schema_version") != 1
                or payload.get("input_fingerprint") != input_fingerprint
                or set(payload.get("speakers") or []) != set(speakers)
                or payload.get("source") != "llm"
            ):
                return None
            result = payload.get("result")
            if not isinstance(result, dict):
                return None
            if not self._speaker_mapping_result_is_valid(result, speakers):
                return None
            return result
        except (OSError, json.JSONDecodeError, TypeError, AttributeError) as exc:
            logger.warning(
                f"说话人映射产物损坏或格式异常，按缓存未命中处理: "
                f"{platform}/{media_id}: {exc}"
            )
            return None

    def save_speaker_mapping(
        self,
        platform: str,
        media_id: str,
        speaker_mapping: Dict[str, Any],
        *,
        input_fingerprint: str,
        speakers: List[str],
        source: str = "llm",
    ) -> None:
        """Atomically save a versioned speaker artifact under the media lock."""
        if source != "llm":
            raise ValueError("completed speaker mappings must have source='llm'")
        # R5 修复（PR3 review hardening）：写入前用读侧同一份深层形状校验
        # 把关（_speaker_mapping_result_is_valid，见该方法文档）——防止
        # 持久化一份 reader 稍后会拒绝读取的畸形产物（非 str 姓名/bool
        # confidence）。校验失败直接 raise（与上面 source 校验同一套
        # "防污染"语义），不落盘；唯一调用方 SpeakerInferencer.infer() 已
        # 有覆盖整个推断流程的 except Exception 兜底（见该方法），会据此
        # 走 identity fallback，不会让任务失败，也不会牵连既有的有效产物。
        if not self._speaker_mapping_result_is_valid(speaker_mapping, speakers):
            raise ValueError(
                f"speaker mapping result failed shape validation, refusing to "
                f"persist a payload the reader would reject: {platform}/{media_id}"
            )
        with self.media_lock(platform, media_id):
            artifact_dir = self._speaker_artifact_dir(platform, media_id)
            if artifact_dir is None:
                raise FileNotFoundError(f"video cache not found: {platform}/{media_id}")
            artifact_dir.mkdir(parents=True, exist_ok=True)
            path = artifact_dir / "speaker_mapping.json"
            temp_path = artifact_dir / f".{path.name}.{uuid.uuid4().hex}.tmp"
            payload = {
                "schema_version": 1,
                "input_fingerprint": input_fingerprint,
                "speakers": list(speakers),
                "source": source,
                "result": speaker_mapping,
            }
            try:
                temp_path.write_text(
                    json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
                    encoding="utf-8",
                )
                os.replace(temp_path, path)
            finally:
                temp_path.unlink(missing_ok=True)

    def invalidate_speaker_mapping(self, platform: str, media_id: str) -> None:
        """删除既有的 speaker_mapping.json，让下一次推断请求视为缓存未命中。

        用于"仅补说话人姓名"场景的回滚（见 api/services/llm_ops.py::
        _refresh_speaker_names_in_existing_structured_artifact 的调用侧）：
        SpeakerInferencer.infer() 在拿到新推断结果后会立即调用
        save_speaker_mapping() 落盘（早于展示产物刷新，见该函数注释），如果
        随后刷新展示产物（llm_processed.json 里的 dialogs）失败，继续保留
        这份已经领先于展示产物的新 mapping 会造成"mapping 已存在、展示未
        更新"的静默不一致——且下一次请求会因为 input_fingerprint 命中这份
        新缓存而跳过重新推断，永远没有机会再次尝试刷新（见
        transcription.py 对 need_speaker_names 的判定：
        get_speaker_mapping(...) 非 None 即视为该层已满足）。

        删除该文件让下一次请求的 get_speaker_mapping() 自然返回 None
        （缓存未命中），从而重新触发一次完整的推断 + 刷新尝试，而不是静默
        维持不一致状态。

        找不到 artifact 目录、或文件本就不存在，都视为"没有需要清理的东西"，
        静默返回；只有真正的文件系统异常才记 warning——回滚本身失败不应该
        掩盖调用方即将抛出的、更重要的刷新失败信号。
        """
        artifact_dir = self._speaker_artifact_dir(platform, media_id)
        if artifact_dir is None:
            return
        path = artifact_dir / "speaker_mapping.json"
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            logger.warning(f"回滚说话人映射缓存失败，忽略: {platform}/{media_id}: {exc}")

    def invalidate_llm_status(
        self, platform: str, media_id: str, use_speaker_recognition: bool
    ) -> Dict[str, Any]:
        """Write-ahead 撤销 llm_status.json：删除前先读出旧内容返回给调用方。

        背景（S1，PR3 review hardening）：_save_llm_results 依次重写多个产物
        文件（校对/总结/结构化），只有全部写入都成功才会走到末尾的
        save_llm_status 调用——任意一次 save_llm_result 失败都会直接
        raise，跳过那次调用。此前的问题是：中途失败后，旧的 llm_status.json
        原样留在磁盘，继续为"新校对文本 + 旧总结/旧结构化"这份此刻并不
        存在过的混合产物背书；下一次请求把它当完整缓存直接返回，静默
        不一致且跳过重试。

        修法：调用方在开始替换任何产物文件之前先调用本方法撤销状态文件——
        任何中途失败都会让产物处于"无状态标记"态，读侧判定（状态缺失=
        未确认完成，见 transcription.py 的分层缓存命中判定）自然触发重试，
        不会再返回混合产物。

        返回撤销前的旧内容，供调用方在全部产物写完后、显式回填"层未
        触碰、按合并语义保留旧值"的字段——不能继续依赖 save_llm_status
        自己读磁盘做 merge：那次读取此刻只会读到本方法刚删除的空文件。

        必须在 media_lock 持有期间调用（media_lock 是 RLock，可重入）：
        与 save_llm_status/save_speaker_mapping 同一把锁保护同一份缓存
        目录，避免撤销和另一个并发写者的重写交错。

        Args:
            platform: 平台名称
            media_id: 媒体ID
            use_speaker_recognition: 本轮实际写入的缓存变体（V1 修复，PR3
                review hardening）——必须与调用方随后 save_llm_result/
                save_llm_status 传入的值一致，否则会撤销/定位到
                不相关的变体（见 _resolve_variant_artifact_dir 文档）。

        Returns:
            Dict[str, Any]: 撤销前的状态内容；文件不存在、内容损坏、或
                找不到对应的缓存目录，均视为"没有旧内容"，返回空 dict。

        Raises:
            OSError: 状态文件确实存在但删除失败（如权限问题）——调用方
                应据此中止整段重写，避免在一个连撤销都做不干净的目录里
                继续写入更多产物，制造更难排查的混合态。
        """
        with self.media_lock(platform, media_id):
            artifact_dir = self._resolve_variant_artifact_dir(
                platform, media_id, use_speaker_recognition,
            )
            if artifact_dir is None:
                return {}
            status_file = artifact_dir / "llm_status.json"
            if not status_file.exists():
                return {}
            old_status: Dict[str, Any] = {}
            try:
                with open(status_file, 'r', encoding='utf-8') as f:
                    old_status = json.load(f)
            except (OSError, json.JSONDecodeError) as read_exc:
                logger.warning(
                    f"撤销前读取旧 llm_status.json 失败，按无旧内容处理: "
                    f"{platform}/{media_id}: {read_exc}"
                )
                old_status = {}
            try:
                status_file.unlink()
            except FileNotFoundError:
                pass
            except OSError as exc:
                logger.error(
                    f"撤销 llm_status.json 失败，中止本轮重写: "
                    f"{platform}/{media_id}: {exc}"
                )
                raise
            return old_status if isinstance(old_status, dict) else {}

    def _atomic_write(self, path: Path, write_fn) -> None:
        """原子写入：先写同目录下的临时文件，写完整后再 os.replace 原地
        替换目标路径；写入过程中任何异常都不会影响原有目标文件——目标
        路径从未被截断/打开写入，temp 文件即便写坏也从未被 rename 上去。

        本地 codex review 第 6 轮 G5：save_cache/save_llm_result 此前直接
        `open(path, "w")` 截断写——写到一半失败（磁盘满、进程被杀、
        json.dump 序列化中途抛错）会把原本完整的旧文件截断成半截或空
        文件，永久丢失已经产出的结果。复用 save_speaker_mapping 已经在用
        的同一套 tmp+os.replace 模式，抽成共享 helper 供文本/JSON 调用方
        复用——JSON 调用方直接把 json.dump(data, f, ...) 作为 write_fn
        传入，保留对文件对象直接写入的原有调用形态，不需要额外维护一份
        "先序列化成字符串再整体写入" 的变体。

        save_llm_status（llm_status.json）与 save_speaker_mapping
        （speaker_mapping.json）此前已经各自实现了等价的 tmp+os.replace
        原子写，本身没有 bug，这里不做无谓改动——保留它们原有实现，只把
        新 helper 用在这一轮真正需要修的裸截断写点上，避免无关改动扩大
        本次修复的影响面。

        Args:
            path: 目标文件最终路径
            write_fn: 接收一个已打开的文本文件对象、把内容写进去的可
                调用对象（如 `lambda f: f.write(text)` 或
                `lambda f: json.dump(data, f, ensure_ascii=False, indent=2)`）。

        Raises:
            Exception: write_fn 或 os.replace 失败时原样向上抛出，调用方
                自行决定如何处理（save_cache/save_llm_result 都已有外层
                try/except，异常会被捕获记录并转换成失败返回值）。临时
                文件在 finally 里无条件清理，不会在缓存目录里留下残留
                .tmp 文件。
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            with open(temp_path, "w", encoding="utf-8") as f:
                write_fn(f)
            os.replace(temp_path, path)
        finally:
            temp_path.unlink(missing_ok=True)

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
                self._atomic_write(llm_file, lambda f: f.write(content))

            elif llm_type == "summary":
                llm_file = file_path / "llm_summary.txt"
                self._atomic_write(llm_file, lambda f: f.write(content))

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

                # 原子写（G5）：json.dump 直接写进 temp 文件对象，序列化
                # 中途抛错也不会影响 llm_file 里已有的旧结果。
                self._atomic_write(
                    llm_file,
                    lambda f: json.dump(structured_data, f, ensure_ascii=False, indent=2),
                )

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
    ) -> Dict[str, Any]:
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
            dict: 锁内完成写入后的完整合并快照
        """
        try:
            # 全程持锁：同一媒体的读-改-写不能被另一个任务的并发调用打断，
            # 否则两个任务各自读到旧快照后先后写回，后写者会用自己那份
            # 缺字段的旧快照覆盖先写者刚合并进去的字段（见 media_lock 文档）。
            with self.media_lock(platform, media_id):
                cache_data = self.get_cache(platform, media_id, use_speaker_recognition=use_speaker_recognition)
                if not cache_data:
                    raise FileNotFoundError(
                        f"cache record not found for {platform}/{media_id}"
                    )

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
                return dict(existing)

        except Exception as e:
            logger.error(f"保存 llm_status.json 失败: {e}")
            raise

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

    def cleanup_old_cache(self, days: int = 30, now: Optional[datetime.datetime] = None) -> int:
        """
        清理旧缓存

        Args:
            days: 保留最近几天的缓存
            now: 计算 cutoff 所用的 UTC 时间基准（tz-aware）。不传（None）时
                内部自行调用 `datetime.datetime.now(datetime.timezone.utc)`，
                向后兼容旧调用方/单元测试。

                codex-review R10 #1：cleanup_old_cache 遍历并删除文件可能耗
                时数秒甚至更久，若调用方随后再独立调用一次
                cleanup_task_status()（其内部再各自调用一次 now()），两次
                cutoff 会因为耗时而不一致，在其间开出一个竞态窗口——某条
                缓存记录的 updated_at 恰好落在窗口内时，缓存清理判定"未到
                cutoff、保留"，但稍晚计算的任务清理 cutoff 已经越过它，判定
                "删除"，从而打破"task_status 至少活得跟 cache 一样久"的不
                变式。调用方（_periodic_maintenance）在同一次维护周期内应
                只计算一次 now，显式传给 cleanup_old_cache 和
                cleanup_task_status 两者共用。

        Returns:
            int: 删除的记录数（不含被下方并发保护跳过、留待下一轮的记录）

        时钟基准（codex-review P2）：video_cache.updated_at 由 SQLite 的
        `CURRENT_TIMESTAMP` 写入，固定为 UTC、无时区后缀的
        "YYYY-MM-DD HH:MM:SS" 格式。cutoff 必须以同样的 UTC 基准计算，否则
        会跟 cleanup_task_status() 的 UTC cutoff 出现时钟偏差：此前这里用
        的是不带时区的本地 `datetime.datetime.now()`，运行环境若在 UTC
        以西时区，本地时间比 UTC 慢，算出的字符串形式 cutoff 会比真实 UTC
        cutoff 更"早"（变相缩短保留期）；以东时区则相反（变相延长保留
        期）。这个偏差还会破坏"task_status 保留期钳制到 cache_retention_
        days"这条不变式的前提——二者本应共用同一把时钟量出的 cutoff，否
        则会在两次清理之间开出一个窗口：task_status 行已按 UTC 基准被删
        （对应 view_token 立即失效），但底层缓存因本地时间偏移还没到这
        里算出的 cutoff、仍然存活（或反过来）。这里改为与
        cleanup_task_status()、AuditLogger.cleanup_old_logs() 一致的写
        法：取带时区的 UTC now 计算 cutoff，再格式化为不带时区后缀的字符
        串参与比较，不依赖 sqlite3 模块的隐式 datetime adapter（该
        adapter 自 Python 3.12 起已弃用）。

        并发保护（U1，PR3 review hardening）：原先按旧 updated_at 选出
        候选行后直接 shutil.rmtree + 删 DB 记录，不持该媒体的 media_lock、
        不复核、不排除在途任务——与 save_llm_status 等同媒体写并发时，会
        掀掉正在使用的目录（活跃任务失败）或删掉刚写完产物的 DB 行（数据
        丢失+孤儿文件）。现改为逐条处理，真正删除前抢占该媒体的 media_lock
        （超时 _CLEANUP_MEDIA_LOCK_TIMEOUT_SECONDS 秒放弃、留给下一轮重试），
        锁内做两次复核：
        1. 该 (platform, media_id) 是否存在非终态 task_status 行（queued/
           processing/calibrating）——覆盖"已排队但还未开始写"的窗口：
           create_task()/recalibrate 路由在真正处理开始前就已经把任务行以
           非终态 INSERT 落库，而 LLM 写路径（llm_ops._save_llm_results /
           CacheManager.save_llm_status）从不刷新 video_cache.updated_at
           （只重写 llm_status.json 等文件），单靠"锁内重查 updated_at"堵不
           住这段窗口，必须显式查 task_status。
        2. 锁内重新查询该行 updated_at 是否仍早于 cutoff——覆盖"等锁期
           间被 save_cache() 重新写入"的窗口（save_cache 是唯一会刷新
           video_cache.updated_at 的写路径，见其 INSERT OR REPLACE ...
           CURRENT_TIMESTAMP）。
        拿锁本身也是一道保护：写者（_save_llm_results/save_llm_status）持锁写入
        期间，这里的 acquire 会天然阻塞等待。
        """
        try:
            effective_now = now if now is not None else datetime.datetime.now(datetime.timezone.utc)
            cutoff_date = (
                effective_now - datetime.timedelta(days=days)
            ).strftime('%Y-%m-%d %H:%M:%S')

            with self._get_cursor() as cursor:
                # 候选集合仅供参考：真正是否删除由下面锁内两次复核决定（见
                # 上方并发保护说明），这里多取 platform/media_id 供逐条加锁
                # 和查询在途任务使用。
                cursor.execute("""
                    SELECT id, platform, media_id, files_loc
                    FROM video_cache
                    WHERE updated_at < ?
                """, (cutoff_date,))
                candidates = cursor.fetchall()

            import shutil

            deleted_count = 0
            for record in candidates:
                record_id = record['id']
                platform = record['platform']
                media_id = record['media_id']

                try:
                    with self.media_lock(
                        platform, media_id,
                        timeout=_CLEANUP_MEDIA_LOCK_TIMEOUT_SECONDS,
                    ):
                        # 复核 1：该媒体是否有非终态任务排队/处理中——
                        # task_status 行在真正写入前就已落库，比 updated_at
                        # 复核更早覆盖"已排队未开始写"的窗口。
                        with self._get_cursor() as cursor:
                            cursor.execute(
                                """
                                SELECT 1 FROM task_status
                                WHERE platform = ? AND media_id = ?
                                  AND status IN (?, ?, ?)
                                LIMIT 1
                                """,
                                (
                                    platform, media_id,
                                    TaskStatus.QUEUED, TaskStatus.PROCESSING,
                                    TaskStatus.CALIBRATING,
                                ),
                            )
                            has_inflight_task = cursor.fetchone() is not None
                        if has_inflight_task:
                            logger.info(
                                f"清理跳过（存在在途任务）: {platform}/{media_id}，留待下一轮"
                            )
                            continue

                        # 复核 2：重新查询该行 updated_at 是否仍早于
                        # cutoff，防止等锁期间被 save_cache() 并发刷新。
                        with self._get_cursor() as cursor:
                            cursor.execute(
                                "SELECT updated_at, files_loc FROM video_cache WHERE id = ?",
                                (record_id,),
                            )
                            fresh_row = cursor.fetchone()
                        if fresh_row is None:
                            # 已被其他调用并发删除，跳过即可
                            continue
                        if fresh_row['updated_at'] >= cutoff_date:
                            logger.info(
                                f"清理跳过（等锁期间被刷新 updated_at）: {platform}/{media_id}"
                            )
                            continue

                        files_loc = fresh_row['files_loc']
                        file_path = self.cache_dir / Path(files_loc)

                        # 引用计数式减法（W3，PR3 review hardening 二轮）：_get_file_path 拼出的目录不含
                        # use_speaker_recognition，同一 (platform, media_id) 的两个变体行（说话人识别开/关）因此天然共享
                        # 同一个 files_loc 目录：删除目录前先查是否存在其他 video_cache 行仍引用同一
                        # files_loc（同媒体的另一个变体行且未被本轮清理命中，或任何原因下巧合
                        # 共享同一目录的行）：有则只删本行 DB 记录、保留目录（新鲜变体的文件因此
                        # 完好无损）；无其他引用才真正 rmtree 整个目录。仍在
                        # 同一把 media_lock(platform, media_id) 临界区内：另一变体
                        # 行的写路径（save_cache）用的是同一把锁，不会跟这里的判断
                        # 产生新的竞争窗口。
                        with self._get_cursor() as cursor:
                            cursor.execute(
                                "SELECT 1 FROM video_cache WHERE files_loc = ? AND id != ? LIMIT 1",
                                (files_loc, record_id),
                            )
                            files_loc_shared = cursor.fetchone() is not None

                        # Y5 修复（PR3 review hardening 加固轮）：DB 行删除提到
                        # rmtree 之前，且两者分属独立的失败域。旧顺序先 rmtree
                        # 后独立事务删 DB 行——DELETE 失败（例如磁盘 I/O 异常、
                        # 数据库被短暂锁住）时目录已经被真删了，DB 行却还留着
                        # 指向一个已经不存在的目录：这行缓存记录从此"查得到、
                        # 读不到"，是确定性的数据丢失；外层 `except Exception:
                        # return 0` 还会把这次失败伪装成"什么都没清理"，掩盖
                        # 实际已经发生的文件删除，连事后追查都做不到。
                        #
                        # 新顺序里 DB DELETE（走 _get_cursor()，提交/回滚都是
                        # 显式事务）先执行——如果它失败，行还在、目录也还没删，
                        # 异常沿 for 循环向上传播、被外层 except 捕获，天然留给
                        # 下一轮 cleanup_old_cache 重试，不会有任何数据丢失。
                        #
                        # rmtree 挪到 DB 行已确认删除之后：此时再失败，只是留下
                        # 一个不再被任何 video_cache 行引用的空目录——无害孤儿
                        # （DB 行已经不在了，不会被误当成有效缓存返回），只记
                        # warning 不 raise，不阻断本轮清理继续处理其它候选行，
                        # 也不让 deleted_count 失真归零。代价：这类孤儿目录目前
                        # 没有独立的兜底清扫器——tempfile_manager 只扫 data/temp
                        # 下的临时任务目录，不扫 cache_dir 下的持久化产物目录；
                        # 只有 rmtree 本身持续失败（如权限被改）时才会累积，规模
                        # 上界是"本轮候选行数"，且新故障模式（无害目录残留）远
                        # 好于旧故障模式（DB 行指向已删目录的确定性数据丢失）。
                        with self._get_cursor() as cursor:
                            cursor.execute("DELETE FROM video_cache WHERE id = ?", (record_id,))

                        if files_loc_shared:
                            logger.info(
                                f"清理仅删 DB 行、保留目录（files_loc 仍被其它变体"
                                f"引用）: {platform}/{media_id}, files_loc={files_loc}"
                            )
                        elif file_path.exists():
                            try:
                                shutil.rmtree(file_path)
                            except OSError as rmtree_exc:
                                logger.warning(
                                    f"DB 行已删除，目录删除失败（留作无害孤儿，暂无"
                                    f"专门兜底清扫器）: {file_path}: {rmtree_exc}"
                                )
                        deleted_count += 1
                except TimeoutError:
                    logger.warning(
                        f"清理跳过（等媒体锁超时，可能正被写入）: {platform}/{media_id}"
                    )
                    continue

            logger.info(f"清理了 {deleted_count} 条旧缓存记录")
            return deleted_count

        except Exception as e:
            logger.error(f"清理缓存失败: {e}")
            return 0

    def cleanup_task_status(
        self,
        retention_days: int,
        cache_retention_days: Optional[int] = None,
        now: Optional[datetime.datetime] = None,
    ) -> int:
        """
        清理过期的终态任务状态记录（task_status 表）

        仅删除已进入终态（success/failed）且完成时间早于保留期的记录；
        非终态任务（queued/processing/calibrating）一律保留，避免误删仍在
        处理中、或崩溃后等待启动恢复扫描（recover_orphaned_tasks）的任务。

        下游消费方保护：task_status 行被 /view/{view_token} 的解析链路依赖；
        审计历史已由 audit.db 自有快照承载。删除前严格完成快照归档与 capability
        过期标记，因此审计保留期不再延长正文访问期。调用方传入
        cache_retention_days 时：
        - 传入值 > 0 且 retention_days 短于它：把生效保留期钳制到该值
          （取二者较大值），并记 warning；
        - 传入值 <= 0（对应下游消费方之一永久保留）：直接跳过清理并记
          warning——只要有一个下游消费方永不过期，task_status 也必须永久
          有效；
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
                0 或负数表示缓存永久保留、跳过清理。调用方也可能传入它与
                其他下游消费方（如 audit_log_retention_days）保留期配置的
                较大值，以确保 task_status 不早于任何一个消费方过期
                （codex-review R10 #2，见 _periodic_maintenance 的钳制目标
                计算）
            now: 计算 cutoff 所用的 UTC 时间基准（tz-aware）。不传（None）
                时内部自行调用 `datetime.datetime.now(datetime.timezone.utc)`，
                向后兼容旧调用方/单元测试；调用方需要与 cleanup_old_cache()
                共用同一个 cutoff 时显式传入（见 cleanup_old_cache 的 now
                参数说明，codex-review R10 #1）

        Returns:
            int: 实际删除的记录数
        """
        try:
            if cache_retention_days is not None:
                if cache_retention_days <= 0:
                    logger.warning(
                        "task_status 清理已跳过：调用方传入的保留期下限<=0，表示对应下游"
                        "消费方 cache 永久保留，而 /view/{view_token} 依赖 "
                        "task_status 行解析，提前删除会造成数据尚在、分享链接已断"
                    )
                    return 0
                if retention_days < cache_retention_days:
                    logger.warning(
                        f"task_status_retention_days({retention_days}) 短于调用方传入的"
                        f"缓存保留期下限({cache_retention_days})，已钳制为该下限："
                        "/view/{view_token} 依赖 task_status 行解析，提前删除会造成"
                        "数据尚在、分享链接已断"
                    )
                    retention_days = cache_retention_days

            effective_now = now if now is not None else datetime.datetime.now(datetime.timezone.utc)
            cutoff = (
                effective_now - datetime.timedelta(days=retention_days)
            ).strftime('%Y-%m-%d %H:%M:%S')

            deleted_count = 0
            while True:
                with self._get_cursor() as cursor:
                    cursor.execute('''
                        SELECT task_id FROM task_status
                        WHERE status IN (?, ?)
                          AND COALESCE(completed_at, created_at) < ?
                        ORDER BY COALESCE(completed_at, created_at)
                        LIMIT 500
                    ''', (TaskStatus.SUCCESS, TaskStatus.FAILED, cutoff))
                    task_ids = [row[0] for row in cursor.fetchall()]
                if not task_ids:
                    break

                for task_id in task_ids:
                    with self._terminal_archive_lock:
                        if self.audit_logger is None:
                            raise RuntimeError(
                                "audit logger is required before deleting terminal tasks"
                            )
                        connection = self._get_connection()
                        cursor = connection.cursor()
                        task = None
                        expired_this_attempt = False
                        try:
                            # Lock cache.db before reading or archiving. A
                            # second process must not retain a stale task dict
                            # and revive its snapshot after this process has
                            # revoked the capability and deleted the task.
                            cursor.execute("BEGIN IMMEDIATE")
                            cursor.execute(
                                "SELECT * FROM task_status WHERE task_id=? "
                                "AND status IN (?, ?) "
                                "AND COALESCE(completed_at, created_at) < ?",
                                (
                                    task_id,
                                    TaskStatus.SUCCESS,
                                    TaskStatus.FAILED,
                                    cutoff,
                                ),
                            )
                            row = cursor.fetchone()
                            if row is None:
                                connection.rollback()
                                continue
                            task = dict(row)
                            for field in ("processing_options", "terminal_snapshot"):
                                if task.get(field):
                                    try:
                                        task[field] = json.loads(task[field])
                                    except (TypeError, json.JSONDecodeError):
                                        task[field] = None
                            # archive -> clear live capability -> delete. The
                            # cache write lock covers the cross-DB sequence;
                            # failures keep the task available for retry.
                            self.audit_logger.archive_task_snapshot(task)
                            # Z2 (PR3 review hardening, this round): capture
                            # expire_task_snapshot's return value instead of
                            # assuming a real transition happened. A retried
                            # cleanup attempt on a task whose snapshot was
                            # already expired by an earlier, crash-interrupted
                            # run (task_status row still present,
                            # content_expired already 1) makes this call a
                            # no-op -- it must NOT be treated as "this attempt
                            # revoked the capability", or a later failure
                            # below would trigger compensation that resurrects
                            # an already-dead capability (see expire_task_
                            # snapshot's own docstring for the full story).
                            expired_this_attempt = self.audit_logger.expire_task_snapshot(task_id)
                            cursor.execute(
                                "DELETE FROM task_status WHERE task_id=? "
                                "AND status IN (?, ?) "
                                "AND COALESCE(completed_at, created_at) < ?",
                                (task_id, TaskStatus.SUCCESS, TaskStatus.FAILED, cutoff),
                            )
                            deleted_count += cursor.rowcount
                            connection.commit()
                        except Exception:
                            connection.rollback()
                            # audit.db commits independently. If capability
                            # expiry succeeded but cache deletion failed, the
                            # still-live task remains authoritative and its
                            # snapshot must be compensated back to live. A
                            # normal retry never revives an expired snapshot --
                            # expired_this_attempt (Z2) is now True only when
                            # *this* call performed the live -> expired
                            # transition, so an already-expired-before-this-
                            # attempt snapshot is correctly left alone here.
                            if expired_this_attempt:
                                try:
                                    # Reacquire the cross-process write lock
                                    # before checking and restoring. Otherwise
                                    # another cleaner can delete the task
                                    # between the check and compensation.
                                    cursor.execute("BEGIN IMMEDIATE")
                                    cursor.execute(
                                        "SELECT 1 FROM task_status WHERE task_id=?",
                                        (task_id,),
                                    )
                                    if cursor.fetchone() is not None and task is not None:
                                        self.audit_logger.restore_live_task_snapshot(task)
                                    connection.commit()
                                except Exception:
                                    connection.rollback()
                                    logger.exception(
                                        "Failed to restore audit snapshot for retained task %s",
                                        task_id,
                                    )
                            raise
                        finally:
                            cursor.close()
                if len(task_ids) < 500:
                    break

            logger.info(f"清理了 {deleted_count} 条超过 {retention_days} 天的终态任务状态记录")
            return deleted_count

        except Exception as e:
            logger.error(f"清理任务状态记录失败: {e}")
            raise

    def close(self):
        """关闭数据库连接"""
        if hasattr(self._local, 'connection'):
            self._local.connection.close()

    def list_terminal_tasks(
        self,
        *,
        limit: int = 500,
        after: Optional[Tuple[Optional[str], str]] = None,
    ) -> List[Dict[str, Any]]:
        """Return at most 500 terminal tasks for audit repair/archive.

        Keyset（seek）分页，取代此前的持久 OFFSET（codex review 第 4 轮 T3，
        见 AuditLogger.repair_task_snapshots 调用侧的详细动机说明）。

        根治动机：OFFSET 是"数第几行"，其含义依赖于查询时集合的当前顺序；
        `cleanup_task_status` 会按 `COALESCE(completed_at, created_at)` 从旧
        到新删除终态任务——与本方法的排序完全同向。若 repair 上一轮停在
        OFFSET=500，下一轮维护周期开始前 cleanup 恰好删掉了最旧的一批行，
        剩余集合会整体左移：原本排在第 500~999 位的任务现在变成排在第
        300~799 位（差值等于被删的行数），resume 时用的还是旧 OFFSET=500，
        实际会从"新顺序下的第 500 位"续扫——把第 300~499 位这一整段任务
        （在被删除之前，它们原本排在第 500~699 位，此前从未被扫到过）
        永久跳过，直到游标绕回 0 重新整轮扫描（若真的会绕回的话）。

        Keyset 分页把"续扫位置"表达成一个具体的值（上一页最后一条记录的
        排序键、task_id），而不是行号：`WHERE (排序键, task_id) > (?, ?)`
        天然只依赖"排在这个值之后"这一关系，与集合中排在这个值*之前*的行
        是否被删除完全无关——cleanup 删掉的行永远只会让"已经在游标之前"
        的这一段变短，绝不会导致游标之后的行被跳过。

        SQLite 行值（row value）比较自 3.15.0（2016）起支持；本项目部署
        目标 `python:3.11-slim`（Debian bookworm）自带 SQLite 3.40+，本地
        开发环境验证为 3.50.4（`sqlite3.sqlite_version_info`），均远高于
        该门槛，故直接使用行值比较写法，不需要展开成
        `排序键 > ? OR (排序键 = ? AND task_id > ?)` 的等价形式。

        排序键为什么是 COALESCE(completed_at, created_at, '') 而不是原始
        completed_at（本地 codex review 第 5 轮 F1 修复）：completed_at
        列本身允许 NULL（历史遗留行/迁移边缘场景），而 SQLite 行值比较
        一旦遇到 NULL 就恒为 unknown（WHERE 中视为 false）。若排序键仍是
        原始 completed_at：(a) 某一页最后一行恰好 completed_at IS NULL
        时，调用方按约定取该行的排序键作为下一页游标，得到 (NULL,
        task_id)——下一页 `(completed_at, task_id) > (NULL, ?)` 对任何行
        都不成立，返回空集，被误判为"整轮扫描完成"；(b) 即便游标本身非
        NULL，只要集合中还有别的 completed_at IS NULL 的行，它们与非
        NULL 游标比较同样恒为 unknown，会被永久排除在所有后续页面之外。
        与 `cleanup_task_status` 同口径改用 COALESCE 后，NULL 行会有一个
        确定、可比较的排序键（回退到 created_at，理论上不应为空的
        NOT-NULL-by-convention 列；用 '' 再兜底一层防御性极端情形），彻底
        消除这两类"NULL 打断 keyset 游标"的场景。

        Args:
            limit: 期望返回的最大行数，仍钳制到 [1, 500]（与此前的 500 上限
                语义保持一致）。
            after: 上一页最后一条记录的 (排序键, task_id)；排序键即
                COALESCE(completed_at, created_at, '') 的值，不是原始
                completed_at。为 None 时从头开始扫描（游标归零后的首轮）。

        Returns:
            按 COALESCE(completed_at, created_at, ''), task_id 升序排列的
            任务字典列表。
        """
        # 排序键/比较键统一用 COALESCE(completed_at, created_at, '') ——
        # 与 cleanup_task_status 同口径（见其 SQL），既处理历史遗留的
        # completed_at IS NULL 终态行，也兜底 created_at 本身异常为空的
        # 极端情形。SQLite 行值比较遇 NULL 时结果恒为 unknown（WHERE 中
        # 视为 false）：若仅在 ORDER BY 侧兜底、WHERE 侧仍用原始
        # completed_at 比较，NULL 行依然会在游标非 NULL 后被永久排除；
        # 若仅在 WHERE 侧兜底、ORDER BY 侧不兜底，则返回顺序与比较基准
        # 不一致，keyset 语义整体失效。两处必须同一个表达式（本地 codex
        # review 第 5 轮 F1）。
        sort_key = "COALESCE(completed_at, created_at, '')"
        bounded = min(max(int(limit), 1), 500)
        with self._get_cursor() as cursor:
            if after is not None:
                after_completed_at, after_task_id = after
                cursor.execute(
                    "SELECT task_id FROM task_status WHERE status IN (?, ?) "
                    f"AND ({sort_key}, task_id) > (?, ?) "
                    f"ORDER BY {sort_key}, task_id LIMIT ?",
                    (
                        TaskStatus.SUCCESS,
                        TaskStatus.FAILED,
                        after_completed_at,
                        after_task_id,
                        bounded,
                    ),
                )
            else:
                cursor.execute(
                    "SELECT task_id FROM task_status WHERE status IN (?, ?) "
                    f"ORDER BY {sort_key}, task_id LIMIT ?",
                    (TaskStatus.SUCCESS, TaskStatus.FAILED, bounded),
                )
            task_ids = [row[0] for row in cursor.fetchall()]
        return [task for task_id in task_ids if (task := self.get_task_by_id(task_id))]
    
    # ========== 任务状态管理方法 ==========
    
    def generate_task_id(self) -> str:
        """生成全局唯一任务ID"""
        return f"task_{uuid.uuid4().hex}"
    
    def generate_view_token(self) -> str:
        """生成查看token"""
        return f"view_{secrets.token_urlsafe(32)}"
    
    def create_task(self, url: str, use_speaker_recognition: bool = False,
                    download_url: str = None, platform: str = None,
                    media_id: str = None, processing_options: Optional[dict] = None,
                    submitted_by: Optional[str] = None,
                    task_id: Optional[str] = None) -> Dict[str, str]:
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
        # task_id 可由调用方预先生成传入（本地 codex review 第 12 轮 P1）：
        # HTTP 路由需要在真正落库之前先拿到 task_id 去注册在进程内在途任务登记表
        # 里（证实有容量才落库、满载拒绝时根本不落库），详见
        # api/routes/tasks.py 的 /api/transcribe 路由。未提供（None，旧调用方的既有行为）时
        # 仍由本方法内部生成。
        task_id = task_id if task_id is not None else self.generate_task_id()
        normalized_options = {
            "calibrate": True,
            "summarize": True,
            "infer_speaker_names": True,
        }
        if processing_options:
            unexpected = set(processing_options) - set(normalized_options)
            if unexpected:
                raise ValueError(f"unknown processing option: {sorted(unexpected)[0]}")
            normalized_options.update(processing_options)
        if any(not isinstance(value, bool) for value in normalized_options.values()):
            raise ValueError("processing_options values must be booleans")

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
                     status, platform, media_id, processing_options, submitted_by)
                    VALUES (?, ?, ?, ?, ?, 'queued', ?, ?, ?, ?)
                ''', (task_id, view_token, url, download_url, use_speaker_recognition,
                      platform, media_id, json.dumps(normalized_options, sort_keys=True),
                      submitted_by))

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
                          calibration_status: str = None, summary_status: str = None,
                          terminal_snapshot: Optional[dict] = None,
                          skip_archive: bool = False) -> bool:
        """
        更新任务状态

        终态黏性:一旦任务写入终态(success/failed),该行永远不会被后续调用覆写——
        下方 WHERE 子句无条件为 status NOT IN ('success','failed')，不读取
        force 的值，防止慢半拍的旧 worker、重复任务或异常重试把已完成的任务
        覆回处理中。force 无法绕过这层保护，见
        test_terminal_snapshot_is_write_once_even_with_force 锁死此行为。

        Args:
            task_id: 任务ID
            status: 状态 (queued/processing/calibrating/success/failed)
            platform: 平台名称
            media_id: 媒体ID
            title: 视频标题
            author: 作者
            cache_id: 关联的缓存ID
            download_url: 实际下载地址
            force: [deprecated，无调用方实际传 True] 当前实现中完全不生效——
                无论传 True 还是 False，终态(success/failed)永远不可覆写，
                非终态之间的转换走的也是同一句无条件 compare-and-set，行为
                完全相同。保留该参数仅为不破坏现有调用签名，不建议新代码
                依赖它；本次仅修正文档，未改动签名/删除参数。
            calibration_status: CalibrationStatus 取值(full/partial/none)，
                "诚实状态模型"落盘到 task_status 表，供 /api/audit/history 等查询消费。
                None 表示不更新该列。
            summary_status: SummaryStatus 取值(generated/skipped_short/failed/pending)，
                None 表示不更新该列。
            skip_archive: 为 True 时跳过终态写入附带的同步审计快照归档
                （archive_task_snapshot）。默认 False 与原有行为一致：正常终态写入
                仍同步归档，保证 /api/audit/history 等查询可以立即看到
                新完成的任务。仅关闭清算（drain_non_terminal_tasks_on_
                shutdown）传 True：关闭路径与仍在跑的维护线程（如
                repair_task_snapshots）共享 _terminal_archive_lock，同步归档可能
                被它阻塞，从根上避开这个锁竞争窗口；跳过的
                归档由下次启动的 repair_task_snapshots 补录（它对已存在
                快照的任务直接跳过，天然幂等）。（本地 codex review
                第 7 轮 H3）
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
                    snapshot = dict(terminal_snapshot or {})
                    snapshot["status"] = status
                    if platform is not None:
                        snapshot["platform"] = platform
                    if media_id is not None:
                        snapshot["media_id"] = media_id
                    if title is not None:
                        snapshot["title"] = title
                    if author is not None:
                        snapshot["author"] = author
                    if calibration_status is not None:
                        snapshot["calibration_status"] = calibration_status
                    if summary_status is not None:
                        snapshot["summary_status"] = summary_status
                    if error_message is not None:
                        snapshot["error_message"] = error_message
                    update_fields.append("terminal_snapshot = ?")
                    params.append(
                        json.dumps(snapshot, ensure_ascii=False, sort_keys=True, default=str)
                    )

                params.append(task_id)

                # 所有转换都使用 compare-and-set。force 只保留为兼容参数，
                # 可用于非终态恢复，但永远不能覆盖已写入的终态快照。
                where = "task_id = ? AND status NOT IN (?, ?)"
                params.extend([TaskStatus.SUCCESS, TaskStatus.FAILED])

                query = f"UPDATE task_status SET {', '.join(update_fields)} WHERE {where}"
                cursor.execute(query, params)

                updated = cursor.rowcount == 1
                if not updated:
                    logger.info(
                        f"任务状态更新被终态黏性拦截(已是终态,跳过): {task_id} -> {status}"
                    )
                else:
                    logger.info(f"任务状态更新: {task_id} -> {status}")

            # Cross-database writes cannot be atomic. Persist the task terminal
            # state first, then best-effort its audit-owned snapshot. A failed
            # snapshot never deletes the task row; startup/maintenance repair
            # can safely retry this idempotent upsert. skip_archive=True (shutdown
            # drain, see the Args docstring above) deliberately skips this
            # synchronous archive step to avoid contending on
            # _terminal_archive_lock with an in-flight maintenance call.
            if (
                updated
                and status in (TaskStatus.SUCCESS, TaskStatus.FAILED)
                and not skip_archive
            ):
                with self._terminal_archive_lock:
                    task = self.get_task_by_id(task_id)
                    if task is not None and self.audit_logger is not None:
                        try:
                            self.audit_logger.archive_task_snapshot(task)
                        except Exception:
                            logger.exception("终态任务审计快照归档失败，将由修复任务重试: %s", task_id)
            return updated

        except Exception as e:
            logger.error(f"更新任务状态失败: {e}")
            raise

    def get_non_terminal_task_ids(self) -> frozenset:
        """返回 task_status 表当前处于非终态（queued/processing/calibrating）
        的 task_id 集合快照。

        用途：RuntimeContext.start() 在进程启动时刻记录这份快照
        （startup_recovery_task_ids），供 _periodic_maintenance 的启动
        恢复重试（recovery_pending 置位时）传给 recover_orphaned_tasks(
        restrict_to_task_ids=...)，精确圈定"本进程启动之前就已存在、且
        启动时仍处于非终态"的这批任务。

        L2 修复（CI review 第 5 轮 P1）：取代此前的 rowid 水位线方案
        （本地 codex review 第 7 轮 H4 引入）——那个方案的前提是 SQLite
        隐式 rowid 严格单调递增，但 task_status 表用 TEXT 主键
        （task_id），没有声明 INTEGER PRIMARY KEY 来接管/别名 rowid，也
        没有 AUTOINCREMENT 关键字防止复用：删除当前持有表内最大 rowid
        的那一行后，下一次插入会复用那个 rowid（SQLite 的默认分配规则是
        "比表内当前最大 rowid 大 1"；曾经的最大值所在行一旦被删除，
        "当前最大"就会回落，新插入行的 rowid 也就跟着回落，可能落回启动
        时记录的水位线以下）。recovery_pending 长期悬而不决、且服务持续
        运行期间任务不断创建/终态化/被 cleanup_task_status 按保留期删除
        的场景下，这足以让一个启动之后才创建的全新任务复用到 <= 水位线
        的 rowid，被下一次恢复重试误判成启动前的僵尸任务、误杀写成
        failed——而这个误判一旦发生，真正在跑的 worker 之后想把它写成
        success 时，会被终态 CAS 挡回去。

        改用显式 task_id 集合从根上消除这整类问题：这份集合在调用的这一
        刻一次性拍下，此后永远不会再增长（不会有新 task_id 被加进来），
        后续任何新创建的任务，无论它的 rowid 如何变化、是否发生过复用，
        只要它的 task_id 不在这份集合里，就不可能被恢复重试误杀。

        Returns:
            frozenset: 当前处于 QUEUED/PROCESSING/CALIBRATING 的
                task_id 集合；空表或全部终态时返回空集合。
        """
        with self._get_cursor() as cursor:
            cursor.execute(
                "SELECT task_id FROM task_status WHERE status IN (?, ?, ?)",
                (TaskStatus.QUEUED, TaskStatus.PROCESSING, TaskStatus.CALIBRATING),
            )
            return frozenset(row[0] for row in cursor.fetchall())

    def _fail_non_terminal_tasks(
        self, *, reason: str, error_message: str, restrict_to_task_ids: Optional[frozenset] = None,
        deadline_seconds: Optional[float] = None, skip_archive: bool = False,
        created_before: Optional[str] = None, exclude_task_ids: Optional[set] = None,
    ) -> int:
        """共享的逐任务终态写入循环，供 recover_orphaned_tasks()（启动恢复）与
        drain_non_terminal_tasks_on_shutdown()（优雅关闭清算）复用。

        两者都要把仍处于 queued/processing/calibrating 的任务经
        update_task_status 的既有 CAS 终态路径写成 failed，附带结构一致的
        terminal_snapshot（只有 reason/error_message 不同）——独立抽出这段
        循环是为了不让"终态必须带不可变快照"这条不变式被两处调用方各自
        实现一遍、其中一处将来悄悄漏掉快照组装或退化成裸 UPDATE。

        逐任务调用 update_task_status（而非一条 UPDATE 覆盖所有行），复用它
        既有的终态快照组装与 CAS 保护。两个场景下待处理任务量通常都很小
        （正常运行时非终态任务不会持续积压），逐条处理的开销可以接受。CAS
        语义因此也保持：update_task_status 自身的
        `WHERE status NOT IN (success, failed)` 保护仍然生效，即使在下面
        SELECT 之后、逐条 UPDATE 之前的窗口期里某个任务被并发地转成了
        终态，也不会被这里覆盖。

        Args:
            reason: 写入 terminal_snapshot["reason"] 的标记
                （"orphaned_on_startup" / "shutdown_drain"）。
            error_message: 写入 task_status.error_message 的说明文本。
            restrict_to_task_ids: 可选的 task_id 白名单（见
                get_non_terminal_task_ids 的详细说明，CI review 第 5 轮
                P1，取代此前基于 rowid 单调性假设、被 rowid 复用打破的
                水位线参数）。传入时只处理 task_id 落在这个集合里的行——
                集合在调用方那一侧一次性拍下、此后不会再增长，天然免疫
                后续任何 rowid 复用。None（默认）保留原有行为：不加限制，
                处理全部非终态行，这是 drain_non_terminal_tasks_on_shutdown
                与启动时首次调用 recover_orphaned_tasks() 的既有用法，
                两者都不需要这层限制。
            deadline_seconds: 可选的总预算（秒），逐任务处理前检查经过的墙钟
                时间，超出预算立即停止并返回已完成的计数（本地 codex review
                第 7 轮 H3）：关闭清算此前无界执行——非终态任务量大、或单个
                任务的终态写入被拖慢（如磁盘 IO 抖动），都可能长时间阻塞
                aclose()，违反"aclose 有界返回"的既定约束。None（默认）保留
                原有行为：不设预算，这是 recover_orphaned_tasks() 的既有
                用法——启动期阻塞是可接受的既有特性，不在本次改动范围内。
                预算耗尽时尚未处理的任务保持非终态，由下一次启动的孤儿恢复
                兜底（语义闭环已经存在，见 recover_orphaned_tasks 的文档）。
            skip_archive: 透传给 update_task_status 的同名参数（见其文档）。
                关闭清算传 True，避开与仍在跑的维护线程共享的
                _terminal_archive_lock 竞争；跳过的归档由下次启动的
                repair_task_snapshots 补录。
            created_before: 可选的 created_at 上界（字符串，格式与
                cleanup_task_status 的 cutoff 相同：SQLite
                CURRENT_TIMESTAMP 的 "YYYY-MM-DD HH:MM:SS" UTC 文本，
                直接做字符串比较）。传入时只扫描 created_at 严格早于
                该值的行——供 reconcile_runtime_orphaned_tasks（本地
                codex review 第 12 轮 P1 发现 c）的运行期对账使用，
                只处理明显超出预期时长的任务，避免误判仍在合理处理
                时间窗口内的任务。None（默认）不限制，保留原有行为。
            exclude_task_ids: 可选的 task_id 排除集合——命中的行即使
                满足上面所有条件也不会被这次调用处理。供
                reconcile_runtime_orphaned_tasks 传入进程内在途任务
                登记表（RuntimeContext.inflight_registry）当前登记的
                task_id，防止把仍在正常处理中的任务误判为孤儿——见
                该方法的详细说明。None（默认）不排除任何行，这是
                recover_orphaned_tasks() 与 drain_non_terminal_tasks_
                on_shutdown() 的既有用法，两者都不需要这层排除。

        Returns:
            int: 被标记为 failed 的任务数。
        """
        # deadline 提前到 SELECT 之前计算 + 连接级 busy_timeout 收窄
        # （本地 codex review 第 12 轮 P2 发现 e）：deadline_seconds 此前
        # 只在下面的 Python 循环里被"尊重"——SELECT 和逐条 UPDATE 各自
        # 底层的 SQLite 调用完全不知道还剩多少预算，连接的 busy_timeout
        # 默认继承 sqlite3.connect() 的 timeout=5.0（~5000ms），单次锁
        # 竞争最坏可以真的等满 5s，即使 deadline_seconds 传入的是远小于
        # 5s 的值（如关闭链路其它阶段已经消耗大半预算后剩下的零头）。
        # 这里只在 deadline_seconds 有值时（即调用方真的要一份有界预算
        # ——目前只有 drain_non_terminal_tasks_on_shutdown 会传）才收紧
        # busy_timeout；recover_orphaned_tasks 的既有无界用法（deadline_
        # seconds=None）不受影响，busy_timeout_bounded 保持 False，跳过
        # 下面所有收紧/复位逻辑，与旧行为完全一致。
        deadline = (
            time.monotonic() + deadline_seconds if deadline_seconds is not None else None
        )
        busy_timeout_bounded = deadline is not None
        if deadline is not None and time.monotonic() >= deadline:
            # 预算在发起任何查询前就已经耗尽（本地 codex review 第 12 轮
            # P2 发现 e：此前初始 SELECT 完全不受 deadline 保护，总在
            # deadline 计算好之前就已经执行）——连第一步 SELECT 都不发起，
            # 未处理的任务留给下一次触发时机兜底（recover_orphaned_tasks
            # 的启动恢复，或 reconcile_runtime_orphaned_tasks 的运行期
            # 对账）。
            logger.warning(
                f"{reason}: 清算预算({deadline_seconds}s)在发起查询前已耗尽，"
                f"整体跳过，未清算的任务留给下一次触发时机兜底"
            )
            return 0

        try:
            if busy_timeout_bounded:
                self._apply_connection_busy_timeout_ms(
                    self._shutdown_drain_busy_timeout_ms(deadline)
                )
            # WHERE 子句只按 status 过滤非终态行——restrict_to_task_ids（L2 修复，CI
            # review 第 5 轮 P1）改在下面 Python 侧按 task_id 白名单过滤（不再拼进 SQL，
            # 详见下方注释），与 created_before 各自独立生效、可以自由组合。
            with self._get_cursor() as cursor:
                where_clauses = ["status IN (?, ?, ?)"]
                params: List[Any] = [
                    TaskStatus.QUEUED, TaskStatus.PROCESSING, TaskStatus.CALIBRATING,
                ]
                if created_before is not None:
                    where_clauses.append("created_at < ?")
                    params.append(created_before)
                cursor.execute(
                    f"SELECT task_id FROM task_status WHERE {' AND '.join(where_clauses)}",
                    params,
                )
                stuck_task_ids = [row[0] for row in cursor.fetchall()]

            # 白名单收紧（L2 修复，CI review 第 5 轮 P1）：同样在 Python 侧过滤而不拼
            # 动态 SQL IN 子句——启动恢复重试场景待处理任务量通常很小，且这份集合本身
            # 就是调用方一次性拍下的快照，没有必要为了省一次 Python 过滤而在 SQL 里
            # 拼接 N 个占位符。restrict_to_task_ids=None（默认）不加这层限制。
            if restrict_to_task_ids is not None:
                stuck_task_ids = [
                    task_id for task_id in stuck_task_ids
                    if task_id in restrict_to_task_ids
                ]

            # 登记表排除（本地 codex review 第 12 轮 P1 发现 c）：在 Python 侧过滤而不拼
            # 动态 SQL IN 子句——两个场景（启动恢复/关闭清算）待处理任务量通常很小，运行期对账
            # 场景在移除上面登记表应关注的任务后剩余量也不大，简单可读
            # 优先于拼接 N 个占位符的 SQL。
            if exclude_task_ids:
                stuck_task_ids = [
                    task_id for task_id in stuck_task_ids
                    if task_id not in exclude_task_ids
                ]

            recovered_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
            count = 0
            for index, task_id in enumerate(stuck_task_ids):
                if deadline is not None and time.monotonic() >= deadline:
                    remaining = len(stuck_task_ids) - index
                    logger.warning(
                        f"{reason}: 清算预算({deadline_seconds}s)耗尽，停止处理，"
                        f"剩余 {remaining} 个非终态任务未清算，将由下次启动的孤儿恢复兜底"
                    )
                    break
                if busy_timeout_bounded:
                    # 每条任务前刷新 busy_timeout（本地 codex review 第 12 轮
                    # P2 发现 e）：剩余预算随循环推进不断缩小，固定在循环外
                    # 设一次会让后面几条任务的单次 UPDATE 仍然拿着一份过大的
                    # busy_timeout，同样可能超出这次调用真实剩余的预算。
                    self._apply_connection_busy_timeout_ms(
                        self._shutdown_drain_busy_timeout_ms(deadline)
                    )
                updated = self.update_task_status(
                    task_id,
                    TaskStatus.FAILED,
                    error_message=error_message,
                    terminal_snapshot={
                        "recovered": True,
                        "reason": reason,
                        "recovered_at": recovered_at,
                    },
                    skip_archive=skip_archive,
                )
                if updated:
                    count += 1
            return count
        finally:
            if busy_timeout_bounded:
                self._apply_connection_busy_timeout_ms(_DEFAULT_SQLITE_BUSY_TIMEOUT_MS)

    def recover_orphaned_tasks(self, restrict_to_task_ids: Optional[frozenset] = None) -> int:
        """启动恢复:将中断的非终态任务标记为 failed，并写入终态快照.

        任务队列存在内存中,进程崩溃/重启时正在处理的任务会随队列丢失,
        DB 里会永远停在 queued/processing/calibrating。启动时调用一次,
        把这些僵尸任务统一标为 failed,避免客户端白白轮询到超时。标 failed
        而非重新入队,避免重复下载/扣费。

        逐任务终态写入循环见 _fail_non_terminal_tasks（与关闭清算共用）。

        restrict_to_task_ids 参数（本地 codex review 第 6 轮 G3 引入
        cutoff，第 7 轮 H4 改为 rowid 水位线语义，CI review 第 5 轮 P1
        发现 rowid 水位线在非 AUTOINCREMENT 表上会被 rowid 复用打破、
        改为显式 task_id 快照）：启动时这一次性调用（app.py::
        startup_event）不需要它——此刻进程刚起、内存队列必然为空，DB
        里任何非终态行都保证是上一个进程崩溃遗留的僵尸，全部扫描安全。
        但如果这次调用本身抛异常（如 audit.db/cache.db 锁争用），
        startup_event 只会记日志、置位 RuntimeContext.recovery_pending
        后继续启动——遗留的非终态任务在服务正常运行期间会永久悬挂，
        除非有后续机制重试。app.py::_periodic_maintenance 提供了这个
        重试：仅当 recovery_pending 置位时才在周期维护里再调用一次本
        方法，但绝不能重新扫描全表——这时进程已经跑了一段时间，自己刚
        受理的新任务也可能正巧处于 queued/processing/calibrating，把
        它们也标 failed 就是"误杀活任务"。因此重试时会带上
        restrict_to_task_ids=RuntimeContext.startup_recovery_task_ids
        （进程启动时刻拍下的 task_status 表非终态 task_id 快照，见
        get_non_terminal_task_ids），只处理这个固定集合里的行——集合
        本身不会再增长，本进程后来创建的新任务，无论其 rowid 是否复用
        了旧行释放出来的编号，task_id 都不可能出现在这份启动前快照里，
        不会被波及。

        Args:
            restrict_to_task_ids: 见上，透传给 _fail_non_terminal_tasks；
                None 表示不限制（启动时首次调用的既有行为）。

        Returns:
            int: 被恢复(标记为 failed)的任务数
        """
        try:
            count = self._fail_non_terminal_tasks(
                reason="orphaned_on_startup",
                error_message="Task interrupted by service restart",
                restrict_to_task_ids=restrict_to_task_ids,
            )
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
            raise

    def drain_non_terminal_tasks_on_shutdown(
        self, *, deadline_seconds: Optional[float] = None,
    ) -> int:
        """优雅关闭清算:进程退出前将仍处于非终态的任务标记为 failed.

        与 recover_orphaned_tasks 是同一类问题的两个触发时机：aclose()/
        close() 取消队列消费者、停掉线程池后，仍停在 queued/processing/
        calibrating 的任务此前会被静默丢弃，只能等下一次启动的孤儿恢复
        才被发现，期间客户端会一直轮询到超时。

        调用方 RuntimeContext._finish_close（见 api/context.py）不再要求
        resources_safe=True 才调用这里（本地 codex review 第 6 轮 G2 修复）
        ——resources_safe=False 的超时路径（尤其是 LLM 排队积压导致
        llm_drained 等待超时，llm_executor.shutdown(cancel_futures=True)
        直接取消掉尚未开始跑的 LLM future，那些任务永远不会走到
        llm_ops._handle_llm_task 里的终态写入）恰恰是最需要这里兜底清算的
        场景。与仍在真实运行的 worker 竞争的取舍：update_task_status 的
        CAS（一旦终态即不可覆盖）保证原子性——若某个仍在运行的 worker
        稍后才真正完成并尝试写 success，而这里已经先把同一任务写成了
        failed，那次真实的 success 会被 CAS 挡回去，等同于把一次本该成功
        的任务误判为失败；但进程本就在退出，任务的产物（若已生成）仍留在
        缓存里，下次同样的请求会直接命中缓存，不会真的丢失工作成果——这
        比"任务永久卡在非终态、客户端轮询到超时"的默认结果更好，因此接受
        这个取舍（详见 _finish_close 的说明）。

        总预算与跳过同步归档（本地 codex review 第 7 轮 H3）：此前本方法
        无期限地逐任务处理——非终态任务量大、或单个任务的终态写入被拖慢
        （如磁盘 IO 抖动），都可能长时间阻塞 aclose()，违反"aclose 有界
        返回"的既定约束；且终态写入默认会同步归档审计快照
        （update_task_status 内部持有 _terminal_archive_lock），若此刻
        恰好有一次维护调用（如 repair_task_snapshots）正持有同一把锁，
        关闭清算会被它阻塞。两个问题的修复：deadline_seconds 给整个清算
        循环设置总预算，逐任务处理前检查剩余预算，超时立即停止（未清算的
        任务保持非终态，由下一次启动的孤儿恢复兜底，语义闭环已经存在）；
        终态写入固定跳过同步归档（_fail_non_terminal_tasks 内部
        skip_archive=True），从根上避开 _terminal_archive_lock 的竞争——
        跳过的归档同样由下次启动的 repair_task_snapshots 补录（它对已存在
        快照的任务直接跳过，天然幂等）。

        本方法自身只做同步阻塞查询，不接触网络/线程；调用方决定是否需要
        兜底捕获异常（关闭路径应当兜底以保证进程能退出，其他调用方——
        目前没有——可以按各自需要处理异常）。

        Args:
            deadline_seconds: 清算循环的总预算（秒），透传给
                _fail_non_terminal_tasks 的同名参数。None（默认）表示不设
                预算，保留原有的无界行为，供直接调用本方法的测试/脚本使用；
                真实关闭路径由 RuntimeContext._drain_non_terminal_tasks_on_
                shutdown 显式传入 WORKER_STOP_TIMEOUT_SECONDS，与
                _stop_workers 的三段有界等待同一量级。

        Returns:
            int: 被清算(标记为 failed)的任务数
        """
        try:
            count = self._fail_non_terminal_tasks(
                reason="shutdown_drain",
                error_message="Task interrupted by service shutdown",
                deadline_seconds=deadline_seconds,
                # 关闭路径固定跳过同步审计快照归档，避开与仍在跑的维护
                # 线程共享的 _terminal_archive_lock 竞争——归档交给下次
                # 启动的 repair_task_snapshots 补录（见上方类文档说明）。
                skip_archive=True,
            )
            if count:
                logger.warning(
                    f"关闭清算:将 {count} 个仍处于非终态"
                    f"(queued/processing/calibrating)的任务标记为 failed"
                )
            else:
                logger.info("关闭清算:无非终态任务需要处理")
            return count
        except Exception as e:
            logger.error(f"关闭清算扫描失败: {e}")
            raise

    def reconcile_runtime_orphaned_tasks(
        self, *, exclude_task_ids: Optional[set] = None,
        grace_period_seconds: float = RUNTIME_RECONCILE_GRACE_SECONDS,
        now: Optional[datetime.datetime] = None,
    ) -> int:
        """运行期对账（本地 codex review 第 12 轮 P1 发现 c）：周期维护
        （app.py::_periodic_maintenance）每轮调用，把"非终态 + 不在进程内
        在途任务登记表 + created_at 早于宽限期"的任务行经既有 CAS 终态
        循环收敛为 failed。

        动机：队列拒绝（task_queue/llm_queue 满）时的清理路径会尝试把已
        落库为 queued/processing 的任务行 CAS 成 failed（见
        api/routes/tasks.py 两处 503 分支），但这次清理写入本身也可能
        失败（如 DB 瞬时锁争用）——此时任务行永久停留在非终态，且不属于
        aclose() 关闭清算或启动孤儿恢复覆盖的任一触发时机（两者只在
        重启/关闭时点各触发一次，服务持续运行期间无人再看这行）。这里
        补上运行期的第三个触发时机，闭合这道此前不存在的运行期收敛缺口。

        双重保护，登记表优先：调用方（_periodic_maintenance）传入
        RuntimeContext.inflight_registry.all_task_ids()（两个 kind 的
        并集）作为 exclude_task_ids——只要任务仍在登记表里，不论运行多久
        都不会被这里判定为孤儿，这是比 created_at 时间阈值更强的保护
        （登记表是进程内实时状态，时间阈值只是它覆盖不到时的第二道保险，
        见 RUNTIME_RECONCILE_GRACE_SECONDS 上方的取值依据）。

        与 recover_orphaned_tasks/drain_non_terminal_tasks_on_shutdown
        共用同一套逐任务 CAS 终态写入循环（_fail_non_terminal_tasks），
        reason 换成 "runtime_reconcile" 以便与另外两个触发时机在
        terminal_snapshot 里区分。

        Args:
            exclude_task_ids: 当前登记表里仍处于受理中/执行中的 task_id
                集合，这些行即使非终态也绝不能被本方法收敛。None 等价于
                空集合（不排除任何行）——调用方原则上总应显式传入登记表
                快照，None 只用作防御性默认值。
            grace_period_seconds: 宽限期（秒），只处理 created_at 早于
                (now - grace_period_seconds) 的行，见上方
                MAX_EXPECTED_TASK_DURATION_SECONDS/
                RUNTIME_RECONCILE_GRACE_SECONDS 的取值依据。
            now: 计算 cutoff 的 UTC 时间基准（tz-aware），None 时内部自取
                datetime.datetime.now(datetime.timezone.utc)（供测试注入，
                与 cleanup_task_status 的 now 参数同一约定）。

        Returns:
            int: 被收敛为 failed 的任务数。
        """
        effective_now = (
            now if now is not None else datetime.datetime.now(datetime.timezone.utc)
        )
        # 与 cleanup_task_status 同款 cutoff 格式（见其文档）：task_status
        # 的 created_at 由 SQLite CURRENT_TIMESTAMP 写入，固定为 UTC、无
        # 时区后缀的 "YYYY-MM-DD HH:MM:SS"，这里用同样格式生成 cutoff 参与
        # 字符串比较，避免依赖 sqlite3 模块的隐式 datetime adapter。
        created_before = (
            effective_now - datetime.timedelta(seconds=grace_period_seconds)
        ).strftime('%Y-%m-%d %H:%M:%S')
        try:
            count = self._fail_non_terminal_tasks(
                reason="runtime_reconcile",
                error_message=(
                    "Task exceeded expected runtime without being tracked as "
                    "in-flight; reconciled by periodic maintenance"
                ),
                created_before=created_before,
                exclude_task_ids=exclude_task_ids or set(),
            )
            if count:
                logger.warning(f"运行期对账：将 {count} 个疑似孤儿任务标记为 failed")
            else:
                logger.info("运行期对账：无需要收敛的任务")
            return count
        except Exception as e:
            logger.error(f"运行期对账失败: {e}")
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
                    task = dict(row)
                    for field in ("processing_options", "terminal_snapshot"):
                        if task.get(field):
                            try:
                                task[field] = json.loads(task[field])
                            except (TypeError, json.JSONDecodeError):
                                logger.warning("Invalid %s JSON for task %s", field, task_id)
                                task[field] = None
                    return task
                return None
                
        except Exception as e:
            logger.error(f"获取任务信息失败: {e}")
            return None
    
    def get_task_by_view_token(self, view_token: str) -> Optional[Dict[str, Any]]:
        """
        根据view_token获取任务信息

        排序策略分三段，不穷举具体状态值，避免状态机演进（新增状态）时腐化：
        - success 永远最高优先级（优先返回成功状态的任务）
        - failed 永远垫底，避免旧的失败记录掩盖同一 view_token 下更新的、
          仍在处理中（queued/processing/calibrating 等）或已成功的任务
        - 其余任何状态（不管是当前已知的还是未来新增的）统一排在中间优先级，
          组内按 created_at DESC 排序，返回最新的一条
        （具体排序表达式见类级常量 _TASK_STATUS_PRIORITY_ORDER_BY）

        K4 修复（本地 codex review 第 8 轮）：此前只取排序后的第一条候选，
        若它的审计快照标记 content_expired 就直接整体返回 None——但同一个
        view_token 下可能存在排序更靠后、仍然有效（未过期）的兄弟任务
        （例如清理流程在"标记过期"与"物理删除"之间崩溃残留、或新任务复用了
        同一 view_token），这条过期候选会把它们一并遮蔽，本该可见的有效
        任务因此查不到。现在改为按排序依次遍历候选，过期的跳过继续看下一
        条，全部过期（或压根没有候选）才返回 None——既保持既有排序语义，
        又不再让单条过期兄弟行拖垮整个 view_token 的可见性。

        Args:
            view_token: 查看token

        Returns:
            Dict: 任务信息
        """
        try:
            with self._get_cursor() as cursor:
                # success 最高优先级；failed 垫底；其余状态居中按最新排序。
                # 不加 LIMIT 1：需要在过期候选上跳过继续看下一条，见上方
                # K4 修复说明。
                cursor.execute(f"""
                    SELECT * FROM task_status
                    WHERE view_token = ?
                    ORDER BY
                        {self._TASK_STATUS_PRIORITY_ORDER_BY}
                """, (view_token,))
                for row in cursor.fetchall():
                    task = dict(row)
                    if self.audit_logger is not None:
                        snapshot = self.audit_logger.get_task_snapshot(task["task_id"])
                        if snapshot and snapshot.get("content_expired"):
                            logger.warning(
                                "跳过已撤销的候选任务，继续查找同 view_token 下的有效任务: "
                                "task_id=%s", task["task_id"]
                            )
                            continue
                    return task
                return None

        except Exception as e:
            logger.error(f"根据view_token获取任务信息失败: {e}")
            return None

    def list_tasks_by_view_token(self, view_token: str) -> List[Dict[str, Any]]:
        """按 view_token 枚举 cache.db task_status 里的全部任务行（任意状态，
        含尚未终态的 queued/processing/calibrating 等），只投影归属判定需要
        的最小字段。

        用途：routes/audit.py::get_task_summary 的 view_token 归属校验
        （本地 codex review 第 5 轮 F2）。此前该校验只兜底
        `task_audit_snapshots` 里同一 view_token 下的其它任务——但那张表
        只有任务终态归档后才有对应行（见 AuditLogger.archive_task_snapshot
        的调用时机：任务完成时/repair_task_snapshots 补录）。同一
        view_token 下，若当前用户自己提交的任务尚未终态（还在排队/处理
        中），它在 task_audit_snapshots 里完全没有行，仅存在于这里
        （cache.db 的 task_status），旧的两级归属检查会漏掉它，导致合法
        提交者查询自己尚在处理中的任务摘要时被误判为 403。

        R1（PR3 review hardening）：跳过已撤销（content_expired）的候选行，
        与 get_task_by_view_token 对同一份 task_status 结果集的处理方式
        （上方方法的 K4 修复）保持同一语义——撤销时 AuditLogger.expire_
        task_snapshot 会把 task_audit_snapshots 里对应行的 view_token 置
        NULL、content_expired 置 1，但 cache.db 这张 task_status 表不受
        影响，撤销后残留的行仍然带着原 submitted_by。若不过滤，这行残留
        证据会被 routes/audit.py::check_view_token_ownership 当作"正面
        归属证据"采信，让已撤销任务的提交者继续凭共享 view_token 读取
        （甚至触发 recalibate）该 view_token 下其他人仍然有效的任务。

        轻量性：`view_token` 列已有索引 `idx_view_token`（见
        `_init_database`），且一个 view_token 下的任务行数通常是个位数
        （同 URL/同 platform+media_id 重复提交才会复用同一个 view_token，
        见 create_task 的查重策略），不会是全表扫描或大结果集；逐行查一次
        审计快照的开销可忽略。

        Args:
            view_token: 查看token

        Returns:
            List[Dict]: 每条为 {"task_id": str, "submitted_by": Optional[str]}，
            已跳过 content_expired 的行，按 task_status 表的自然顺序返回，
            不保证排序（调用方只需要做成员归属判定，不关心顺序）。查询异常
            时返回空列表并记录日志，与本类其它只读查询方法（如
            get_task_by_id）的 fail-safe 约定一致——调用方（归属校验）在
            拿不到数据时会退回既有的两级检查，不会因为这里异常而整体失败。
        """
        try:
            with self._get_cursor() as cursor:
                cursor.execute(
                    "SELECT task_id, submitted_by FROM task_status WHERE view_token = ?",
                    (view_token,),
                )
                rows = cursor.fetchall()

            results = []
            for row in rows:
                task_id, submitted_by = row[0], row[1]
                if self.audit_logger is not None:
                    snapshot = self.audit_logger.get_task_snapshot(task_id)
                    if snapshot and snapshot.get("content_expired"):
                        logger.warning(
                            "跳过已撤销的候选任务，不作为 view_token 归属证据: "
                            "task_id=%s", task_id
                        )
                        continue
                results.append({"task_id": task_id, "submitted_by": submitted_by})
            return results
        except Exception as e:
            logger.error(f"按view_token枚举任务失败: {e}")
            return []

    def task_exists(self, task_id: str) -> bool:
        """Check task existence for cross-database cleanup; database errors propagate."""
        with self._get_cursor() as cursor:
            cursor.execute("SELECT 1 FROM task_status WHERE task_id=?", (task_id,))
            return cursor.fetchone() is not None
    
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

        排序策略分三段，不穷举具体状态值，避免状态机演进（新增状态）时腐化：
        - success 永远最高优先级（优先返回成功完成的任务）
        - failed 永远垫底，避免旧的失败记录掩盖同一 URL 下更新的、
          仍在处理中（queued/processing/calibrating 等）或已成功的任务
        - 其余任何状态（不管是当前已知的还是未来新增的）统一排在中间优先级，
          组内按 created_at DESC 排序，返回最新的一条
        （具体排序表达式见类级常量 _TASK_STATUS_PRIORITY_ORDER_BY）

        Args:
            url: 视频URL
            use_speaker_recognition: 是否使用说话人识别

        Returns:
            Optional[Dict]: 现有任务信息（包含task_id和view_token），如果没有找到则返回None
        """
        try:
            with self._get_cursor() as cursor:
                # 查找相同URL和说话人识别设置的任务
                # success 最高优先级；failed 垫底；其余状态居中按最新排序
                cursor.execute(f'''
                    SELECT task_id, view_token, url, download_url, use_speaker_recognition, status,
                           title, author, platform, media_id, cache_id, created_at
                    FROM task_status
                    WHERE url = ? AND use_speaker_recognition = ?
                    ORDER BY
                        {self._TASK_STATUS_PRIORITY_ORDER_BY}
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

        排序策略分三段，不穷举具体状态值，避免状态机演进（新增状态）时腐化：
        - success 永远最高优先级（优先返回成功完成的任务）
        - failed 永远垫底，避免旧的失败记录掩盖同一 (platform, media_id) 下
          更新的、仍在处理中（queued/processing/calibrating 等）或已成功的任务
        - 其余任何状态（不管是当前已知的还是未来新增的）统一排在中间优先级，
          组内按 created_at DESC 排序，返回最新的一条
        （具体排序表达式见类级常量 _TASK_STATUS_PRIORITY_ORDER_BY）

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
                # success 最高优先级；failed 垫底；其余状态居中按最新排序
                cursor.execute(f'''
                    SELECT task_id, view_token, url, download_url, use_speaker_recognition, status,
                           title, author, platform, media_id, cache_id, created_at
                    FROM task_status
                    WHERE platform = ? AND media_id = ? AND use_speaker_recognition = ?
                    ORDER BY
                        {self._TASK_STATUS_PRIORITY_ORDER_BY}
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
