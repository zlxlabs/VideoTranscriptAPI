"""
API调用审计日志模块

提供API调用统计和审计功能，支持多用户监控。
使用线程本地存储复用数据库连接，内置 schema 版本迁移系统。
"""

import sqlite3
import threading
import json
from contextlib import nullcontext
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional, Dict, Any, List
from .logger import setup_logger

logger = setup_logger("audit_logger")

# Schema 版本号，每次表结构变更时递增
CURRENT_SCHEMA_VERSION = 5


class AuditLogger:
    """API调用审计日志记录器

    使用 threading.local() 管理每线程独立的 SQLite 连接，
    避免每次请求新建/关闭连接的开销。
    """

    def __init__(self, db_path: str = None):
        """
        初始化审计日志记录器

        Args:
            db_path: SQLite数据库文件路径，默认为 data/audit.db
        """
        if db_path is None:
            project_root = Path(__file__).resolve().parents[4]
            data_dir = project_root / "data"
            data_dir.mkdir(parents=True, exist_ok=True)
            db_path = data_dir / "audit.db"

        self.db_path = str(db_path)
        self._local = threading.local()
        # Keyset（seek）分页游标：上一轮 repair_task_snapshots 扫到的最后一条
        # 记录的 (completed_at, task_id)；None 表示从头开始（见
        # repair_task_snapshots 与 CacheManager.list_terminal_tasks 的详细
        # 动机说明——取代此前的持久 OFFSET，避免并发删除导致的跳页/饥饿）。
        self._repair_after: Optional[tuple] = None
        self.repair_scan_complete = False
        self._init_database()
        logger.info(f"审计日志记录器初始化完成，数据库路径: {self.db_path}")

    def _get_connection(self) -> sqlite3.Connection:
        """获取当前线程的数据库连接（复用已有连接）"""
        if not hasattr(self._local, 'connection'):
            self._local.connection = sqlite3.connect(self.db_path)
            # 启用 WAL 模式提升并发读写性能
            try:
                self._local.connection.execute("PRAGMA journal_mode=WAL")
            except sqlite3.OperationalError:
                logger.warning("WAL mode not supported, using default journal mode")
        return self._local.connection

    def close(self) -> None:
        """Close the connection owned by the current runtime thread."""
        connection = getattr(self._local, "connection", None)
        if connection is not None:
            connection.close()
            del self._local.connection

    @contextmanager
    def _get_cursor(self):
        """获取数据库游标的上下文管理器，自动 commit/rollback"""
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
        """初始化数据库表结构并执行必要的 schema 迁移"""
        try:
            with self._get_cursor() as cursor:
                # 创建 schema 版本表
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS schema_version (
                        version INTEGER NOT NULL
                    )
                ''')
                self._check_and_migrate(cursor)
                logger.info("API审计日志数据库初始化完成")
        except Exception as e:
            logger.error(f"初始化审计日志数据库失败: {str(e)}")
            raise

    def _get_schema_version(self, cursor) -> int:
        """获取当前 schema 版本号

        Args:
            cursor: 数据库游标

        Returns:
            int: 当前版本号，无记录则返回 0
        """
        cursor.execute("SELECT version FROM schema_version LIMIT 1")
        row = cursor.fetchone()
        return row[0] if row else 0

    def _set_schema_version(self, cursor, version: int):
        """设置 schema 版本号

        Args:
            cursor: 数据库游标
            version: 要设置的版本号
        """
        cursor.execute("DELETE FROM schema_version")
        cursor.execute("INSERT INTO schema_version (version) VALUES (?)", (version,))

    def _check_and_migrate(self, cursor):
        """检查 schema 版本并按需执行迁移

        Args:
            cursor: 数据库游标
        """
        version = self._get_schema_version(cursor)

        if version < 1:
            self._migrate_v1(cursor)

        if version < 2:
            self._migrate_v2(cursor)

        if version < 3:
            self._migrate_v3(cursor)

        if version < 4:
            self._migrate_v4(cursor)

        if version < 5:
            self._migrate_v5(cursor)

        if version < CURRENT_SCHEMA_VERSION:
            self._set_schema_version(cursor, CURRENT_SCHEMA_VERSION)
            logger.info(f"Schema 迁移完成: v{version} -> v{CURRENT_SCHEMA_VERSION}")

    def _migrate_v1(self, cursor):
        """v1 迁移：创建初始表结构和索引"""
        logger.info("执行 schema 迁移 v1: 创建审计日志表")

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS api_audit_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                api_key_masked TEXT NOT NULL,
                user_id TEXT,
                endpoint TEXT NOT NULL,
                video_url TEXT,
                request_time DATETIME DEFAULT CURRENT_TIMESTAMP,
                processing_time_ms INTEGER,
                status_code INTEGER,
                task_id TEXT,
                user_agent TEXT,
                remote_ip TEXT
            )
        ''')

        cursor.execute('CREATE INDEX IF NOT EXISTS idx_api_key ON api_audit_logs(api_key_masked)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_user_id ON api_audit_logs(user_id)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_request_time ON api_audit_logs(request_time)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_endpoint ON api_audit_logs(endpoint)')

    def _migrate_v2(self, cursor):
        """v2 迁移：新增 wechat_webhook 列，用于记录任务提交时使用的通知 webhook 地址"""
        logger.info("执行 schema 迁移 v2: 新增 wechat_webhook 列")
        cursor.execute(
            "ALTER TABLE api_audit_logs ADD COLUMN wechat_webhook TEXT"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_wechat_webhook ON api_audit_logs(wechat_webhook)"
        )

    def _migrate_v3(self, cursor):
        """v3 迁移：新增 llm_usage 表，记录 LLM 调用 token 用量（按任务/阶段审计）

        每行对应一次 LLMClient.call() 调用，记录 prompt/completion/total token
        用量、耗时、所属任务 task_id 与处理阶段 stage。provider 未回报 usage 时
        仍写入一行（token 记 0），并通过 usage_missing 标记，避免静默丢弃。
        """
        logger.info("执行 schema 迁移 v3: 创建 llm_usage 表")
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS llm_usage (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT NOT NULL,
                stage TEXT NOT NULL,
                model TEXT NOT NULL,
                prompt_tokens INTEGER NOT NULL DEFAULT 0,
                completion_tokens INTEGER NOT NULL DEFAULT 0,
                total_tokens INTEGER NOT NULL DEFAULT 0,
                duration_ms INTEGER NOT NULL DEFAULT 0,
                usage_missing INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            )
        ''')
        cursor.execute(
            'CREATE INDEX IF NOT EXISTS idx_llm_usage_task_id ON llm_usage(task_id)'
        )
        cursor.execute(
            'CREATE INDEX IF NOT EXISTS idx_llm_usage_created_at ON llm_usage(created_at)'
        )

    def _migrate_v4(self, cursor):
        """Create audit-owned immutable task metadata snapshots."""
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS task_audit_snapshots (
                task_id TEXT PRIMARY KEY,
                view_token TEXT,
                title TEXT,
                author TEXT,
                platform TEXT,
                status TEXT NOT NULL,
                calibration_status TEXT,
                summary_status TEXT,
                submitted_by TEXT,
                processing_options TEXT,
                completed_at TEXT,
                content_expired INTEGER NOT NULL DEFAULT 0,
                archived_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_audit_task_id ON api_audit_logs(task_id)"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_audit_user_time "
            "ON api_audit_logs(user_id, request_time DESC)"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_snapshot_platform ON task_audit_snapshots(platform)"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_snapshot_author ON task_audit_snapshots(author)"
        )

    def _migrate_v5(self, cursor):
        """v5: task_audit_snapshots.chapters_status for chapter outline honesty model."""
        logger.info("执行 schema 迁移 v5: task_audit_snapshots.chapters_status")
        cursor.execute("PRAGMA table_info(task_audit_snapshots)")
        columns = [col[1] for col in cursor.fetchall()]
        if "chapters_status" not in columns:
            cursor.execute(
                "ALTER TABLE task_audit_snapshots ADD COLUMN chapters_status TEXT"
            )

    def archive_task_snapshot(self, task: Dict[str, Any]) -> None:
        """Idempotently copy task metadata without reviving revoked capabilities."""
        self._write_task_snapshot(task, revive_expired=False)

    def restore_live_task_snapshot(self, task: Dict[str, Any]) -> None:
        """Compensate a failed deletion after confirming the task still exists."""
        self._write_task_snapshot(task, revive_expired=True)

    def _write_task_snapshot(
        self, task: Dict[str, Any], *, revive_expired: bool
    ) -> None:
        task_id = task.get("task_id")
        if not task_id:
            raise ValueError("task snapshot requires task_id")
        options = task.get("processing_options")
        if options is not None and not isinstance(options, str):
            options = json.dumps(options, ensure_ascii=False, sort_keys=True)
        if revive_expired:
            view_token_update = "excluded.view_token"
            expired_update = "0"
        else:
            view_token_update = (
                "CASE WHEN task_audit_snapshots.content_expired=1 "
                "THEN NULL ELSE excluded.view_token END"
            )
            expired_update = "task_audit_snapshots.content_expired"
        with self._get_cursor() as cursor:
            cursor.execute(f'''
                INSERT INTO task_audit_snapshots
                (task_id, view_token, title, author, platform, status,
                 calibration_status, summary_status, chapters_status, submitted_by,
                 processing_options, completed_at, content_expired, archived_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, CURRENT_TIMESTAMP)
                ON CONFLICT(task_id) DO UPDATE SET
                    view_token={view_token_update},
                    title=excluded.title,
                    author=excluded.author,
                    platform=excluded.platform,
                    status=excluded.status,
                    calibration_status=excluded.calibration_status,
                    summary_status=excluded.summary_status,
                    chapters_status=excluded.chapters_status,
                    submitted_by=excluded.submitted_by,
                    processing_options=excluded.processing_options,
                    completed_at=excluded.completed_at,
                    content_expired={expired_update},
                    archived_at=CURRENT_TIMESTAMP
            ''', (
                task_id, task.get("view_token"), task.get("title"), task.get("author"),
                task.get("platform"), task.get("status") or "unknown",
                task.get("calibration_status"), task.get("summary_status"),
                task.get("chapters_status"),
                task.get("submitted_by"), options, task.get("completed_at"),
            ))

    def expire_task_snapshot(self, task_id: str) -> bool:
        """Remove the live capability before deleting its task row.

        Z2 (PR3 review hardening, this round): the WHERE clause now
        requires the snapshot to still be live (COALESCE(content_expired,
        0) = 0), and the return value reports whether *this call* actually
        performed the live -> expired transition (rowcount > 0) versus a
        no-op on an already-expired (or nonexistent) row. This call used
        to be unconditional and void -- cache_manager.py's cleanup loop
        set `expired_this_attempt = True` right after calling it,
        regardless of whether a real transition happened. On a retried
        cleanup attempt where a task's snapshot had already been expired
        by an earlier, crash-interrupted run (task_status row still
        present, content_expired already 1), this call is a pure no-op,
        but the caller still believed *this* attempt was the one that
        revoked the capability. If the subsequent DELETE FROM task_status
        then failed too, the failure-compensation branch would call
        restore_live_task_snapshot(revive_expired=True) and resurrect a
        capability that was legitimately already dead -- breaking
        revocation monotonicity. Callers must use this return value (not
        just "no exception raised") to decide whether compensation is
        appropriate.

        Returns:
            bool: True if this call transitioned the snapshot from live to
            expired; False if it was already expired or the row doesn't
            exist (idempotent no-op).
        """
        with self._get_cursor() as cursor:
            cursor.execute(
                "UPDATE task_audit_snapshots "
                "SET view_token=NULL, content_expired=1 "
                "WHERE task_id=? AND COALESCE(content_expired, 0) = 0",
                (task_id,),
            )
            return cursor.rowcount > 0

    def get_task_snapshot(self, task_id: str) -> Optional[Dict[str, Any]]:
        with self._get_cursor() as cursor:
            cursor.execute(
                "SELECT * FROM task_audit_snapshots WHERE task_id=?", (task_id,)
            )
            row = cursor.fetchone()
            if row is None:
                return None
            columns = [description[0] for description in cursor.description]
        result = dict(zip(columns, row))
        if result.get("processing_options"):
            result["processing_options"] = json.loads(result["processing_options"])
        result["content_expired"] = bool(result["content_expired"])
        return result

    def repair_task_snapshots(self, cache_manager, limit: int = 500) -> int:
        """Idempotently archive a bounded batch of terminal tasks.

        跨进程复活过期 view_token 的窗口（本地 codex review 追加发现，见
        docs/sessions/260716-pr3x-gate/REVIEW-LOG.md 的核实结论）：`tasks`
        由上面 `list_terminal_tasks` 一次性拉取，其中每个 task dict 都是
        "那一刻" cache.db 的快照。若在本方法遍历这批任务、真正调用
        `archive_task_snapshot` 之前的这段时间里，另一个进程/协程已经完整
        跑完 `cache_manager.cleanup_task_status`（归档→expire→删除 task_status
        行）*并且* `cleanup_old_logs` 的墓碑清理也已把这个 task_id 的
        `task_audit_snapshots` 行彻底删除（该行确实会被删——见
        `AuditLogger.cleanup_old_logs`，`content_expired=1` 不是永久保留，
        只是多一层 `task_exists` 门槛），那么这里 `get_task_snapshot` 会看到
        `None`（而不是一条 `content_expired=1` 的墓碑），从而误判"从未归档过"，
        用手里这份过时的 task dict（仍带着已被吊销的 view_token）重新
        INSERT 出一条 `content_expired=0` 的快照——复活一个已经吊销的
        view_token。

        修复：不再信任 `list_terminal_tasks` 早先拉取的 task dict，归档前
        用 `cache_manager.get_task_by_id` 重新确认任务此刻是否仍然存在。
        cache.db/audit.db 是两个独立文件，做不到真正跨库事务，这里只能
        把"过时快照"的窗口从"整批任务的遍历耗时"收紧到"这一条任务归档前的
        一次重新查询"——重新查询后仍可能存在极小的 TOCTOU 窗口，但这已经是
        跨数据库场景下能做到的最小正确修法（与 cleanup_task_status 自身
        "归档前重新 SELECT 一次任务行"的既有模式一致）。若重新查询发现任务
        已经不存在，直接跳过：任务已被删除意味着它的快照要么已经被
        cleanup_task_status 正确归档+expire 过，要么本来就不需要被归档。

        跳页/饥饿（本地 codex review 第 4 轮 T3）：`_repair_after` 是一个
        keyset（seek）游标——上一轮最后一条记录的 (排序键, task_id)，不是
        行号。`cleanup_task_status` 与本方法按同一个顺序（排序键, task_id
        升序）扫描，且总是删除排在前面（更旧）的行；keyset 游标只表达
        "排在这个值之后"，与游标之前的行是否被删除无关，因此不会像持久
        OFFSET 那样因为集合整体左移而把一整批尚未扫描过的任务永久跳过
        （详见 CacheManager.list_terminal_tasks 的 docstring）。500 上限与
        "snapshot 已存在即跳过归档"的既有语义不变；一轮扫完（返回行数 <
        limit）后游标归零，下一次从头重新扫描。

        游标取值不能是原始 completed_at（本地 codex review 第 5 轮 F1
        修复）：completed_at 允许 NULL（历史遗留行），若直接用它构造游标，
        某一页恰好在 completed_at IS NULL 的行结束时会存下 (None,
        task_id)——下一轮 CacheManager.list_terminal_tasks 的 SQL 行值
        比较遇到 NULL 恒为 unknown，返回空集，被误判为"整轮扫描完成"，
        导致这条 NULL 行之后的所有终态任务永久饿死（错误证据/回归测试见
        test_repair_keyset_cursor_survives_null_completed_at）。这里改用
        `completed_at or created_at or ""`，与 CacheManager 侧
        `COALESCE(completed_at, created_at, '')` 是同一个排序键表达式的
        Python 等价写法——两处任何一处单独修都不足以根治：只修 SQL 侧，
        游标本身仍可能是 None，一样触发上述空集误判；只修游标构造，
        SQL 比较仍用原始 completed_at 列，其余 NULL 行依旧会被永久排除。
        """
        lock_factory = getattr(cache_manager, "terminal_archive_lock", None)
        with lock_factory() if lock_factory else nullcontext():
            bounded = min(max(limit, 1), 500)
            tasks = cache_manager.list_terminal_tasks(
                limit=bounded, after=self._repair_after
            )
            archived = 0
            for task in tasks:
                task_id = task["task_id"]
                snapshot = self.get_task_snapshot(task_id)
                if snapshot is None:
                    current_task = cache_manager.get_task_by_id(task_id)
                    if current_task is not None:
                        self.archive_task_snapshot(current_task)
                        archived += 1
            if len(tasks) < bounded:
                self._repair_after = None
                self.repair_scan_complete = True
            else:
                last_task = tasks[-1]
                sort_key = last_task.get("completed_at") or last_task.get("created_at") or ""
                self._repair_after = (sort_key, last_task["task_id"])
                self.repair_scan_complete = False
            return archived

    def _mask_api_key(self, api_key: str) -> str:
        """
        对API密钥进行脱敏处理

        Args:
            api_key: 原始API密钥

        Returns:
            str: 脱敏后的API密钥（保留前4位和后4位）
        """
        if not api_key or len(api_key) < 8:
            return "****"

        return f"{api_key[:4]}{'*' * (len(api_key) - 8)}{api_key[-4:]}"

    def log_api_call(self,
                     api_key: str,
                     user_id: Optional[str],
                     endpoint: str,
                     video_url: Optional[str] = None,
                     processing_time_ms: Optional[int] = None,
                     status_code: Optional[int] = None,
                     task_id: Optional[str] = None,
                     user_agent: Optional[str] = None,
                     remote_ip: Optional[str] = None,
                     wechat_webhook: Optional[str] = None) -> bool:
        """
        记录API调用日志

        Args:
            api_key: API密钥
            user_id: 用户ID
            endpoint: 请求端点
            video_url: 视频URL（可选）
            processing_time_ms: 处理耗时（毫秒）
            status_code: HTTP状态码
            task_id: 任务ID
            user_agent: 用户代理
            remote_ip: 客户端IP
            wechat_webhook: 任务提交时使用的通知 webhook 地址（可选）

        Returns:
            bool: 记录是否成功
        """
        try:
            api_key_masked = self._mask_api_key(api_key)

            with self._get_cursor() as cursor:
                cursor.execute('''
                    INSERT INTO api_audit_logs
                    (api_key_masked, user_id, endpoint, video_url, processing_time_ms,
                     status_code, task_id, user_agent, remote_ip, wechat_webhook)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (
                    api_key_masked, user_id, endpoint, video_url, processing_time_ms,
                    status_code, task_id, user_agent, remote_ip, wechat_webhook
                ))

            logger.debug(f"API调用日志记录成功: {endpoint}, 用户: {user_id}")
            return True

        except Exception as e:
            logger.error(f"记录API调用日志失败: {str(e)}")
            return False

    def get_user_stats(self, user_id: str, days: int = 30) -> Dict[str, Any]:
        """
        获取指定用户的使用统计

        Args:
            user_id: 用户ID
            days: 统计天数，默认30天

        Returns:
            dict: 用户使用统计信息
        """
        try:
            # 使用 Python 计算截止日期，避免 SQL 格式化注入
            cutoff_date = (datetime.now(timezone.utc) - timedelta(days=days)).strftime('%Y-%m-%d %H:%M:%S')

            with self._get_cursor() as cursor:
                # 查询指定天数内的统计数据
                cursor.execute('''
                    SELECT
                        COUNT(*) as total_calls,
                        COUNT(DISTINCT DATE(request_time)) as active_days,
                        AVG(processing_time_ms) as avg_processing_time,
                        MAX(request_time) as last_call_time,
                        MIN(request_time) as first_call_time
                    FROM api_audit_logs
                    WHERE user_id = ?
                    AND request_time >= ?
                ''', (user_id, cutoff_date))

                stats = cursor.fetchone()

                # 查询端点使用统计
                cursor.execute('''
                    SELECT endpoint, COUNT(*) as count
                    FROM api_audit_logs
                    WHERE user_id = ?
                    AND request_time >= ?
                    GROUP BY endpoint
                    ORDER BY count DESC
                ''', (user_id, cutoff_date))

                endpoint_stats = cursor.fetchall()

                # 查询状态码统计
                cursor.execute('''
                    SELECT status_code, COUNT(*) as count
                    FROM api_audit_logs
                    WHERE user_id = ?
                    AND request_time >= ?
                    AND status_code IS NOT NULL
                    GROUP BY status_code
                    ORDER BY count DESC
                ''', (user_id, cutoff_date))

                status_stats = cursor.fetchall()

            return {
                "user_id": user_id,
                "days": days,
                "total_calls": stats[0] if stats[0] else 0,
                "active_days": stats[1] if stats[1] else 0,
                "avg_processing_time_ms": round(stats[2], 2) if stats[2] else 0,
                "last_call_time": stats[3],
                "first_call_time": stats[4],
                "endpoint_stats": [{"endpoint": ep[0], "count": ep[1]} for ep in endpoint_stats],
                "status_stats": [{"status_code": st[0], "count": st[1]} for st in status_stats]
            }

        except Exception as e:
            logger.error(f"获取用户统计失败: {str(e)}")
            return {"error": str(e)}

    def get_all_users_stats(self, days: int = 30) -> List[Dict[str, Any]]:
        """
        获取所有用户的使用统计

        Args:
            days: 统计天数，默认30天

        Returns:
            list: 所有用户的使用统计列表
        """
        try:
            # 使用 Python 计算截止日期，避免 SQL 格式化注入
            cutoff_date = (datetime.now(timezone.utc) - timedelta(days=days)).strftime('%Y-%m-%d %H:%M:%S')

            with self._get_cursor() as cursor:
                cursor.execute('''
                    SELECT DISTINCT user_id
                    FROM api_audit_logs
                    WHERE user_id IS NOT NULL
                    AND request_time >= ?
                ''', (cutoff_date,))

                user_ids = [row[0] for row in cursor.fetchall()]

            # 获取每个用户的统计（在锁外调用，避免死锁）
            all_stats = []
            for uid in user_ids:
                user_stats = self.get_user_stats(uid, days)
                if "error" not in user_stats:
                    all_stats.append(user_stats)

            return all_stats

        except Exception as e:
            logger.error(f"获取所有用户统计失败: {str(e)}")
            return []

    def get_recent_calls(self, user_id: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        """
        获取最近的API调用记录

        Args:
            user_id: 用户ID，为空则获取所有用户的记录
            limit: 返回记录数量限制

        Returns:
            list: API调用记录列表
        """
        try:
            with self._get_cursor() as cursor:
                if user_id:
                    cursor.execute('''
                        SELECT api_key_masked, user_id, endpoint, video_url,
                               request_time, processing_time_ms, status_code,
                               task_id, user_agent, remote_ip
                        FROM api_audit_logs
                        WHERE user_id = ?
                        ORDER BY request_time DESC
                        LIMIT ?
                    ''', (user_id, limit))
                else:
                    cursor.execute('''
                        SELECT api_key_masked, user_id, endpoint, video_url,
                               request_time, processing_time_ms, status_code,
                               task_id, user_agent, remote_ip
                        FROM api_audit_logs
                        ORDER BY request_time DESC
                        LIMIT ?
                    ''', (limit,))

                rows = cursor.fetchall()

            calls = []
            for row in rows:
                calls.append({
                    "api_key_masked": row[0],
                    "user_id": row[1],
                    "endpoint": row[2],
                    "video_url": row[3],
                    "request_time": row[4],
                    "processing_time_ms": row[5],
                    "status_code": row[6],
                    "task_id": row[7],
                    "user_agent": row[8],
                    "remote_ip": row[9]
                })

            return calls

        except Exception as e:
            logger.error(f"获取最近API调用记录失败: {str(e)}")
            return []

    def cleanup_old_logs(self, days: int = 90, task_exists=None) -> int:
        """
        清理指定天数之前的日志记录（api_audit_logs + llm_usage，同一 cutoff）

        llm_usage（schema v3 新增的 LLM token 用量审计表，见 usage_recorder.py）
        此前从未被任何清理逻辑覆盖——每次 LLM 调用都会插一行，配置了
        audit_log_retention_days 也拦不住它无限增长（codex-review R4 #3）。
        两张表的时间列都用同一个 `%Y-%m-%d %H:%M:%S` strftime 格式落库
        （见 log_api_call 的 request_time 与 usage_recorder.record 的
        created_at），因此直接复用同一个 cutoff_date，保持清理口径一致。

        Args:
            days: 保留天数，默认90天

        Returns:
            int: 删除的记录数量（两张表合计）
        """
        try:
            cutoff_date = (datetime.now(timezone.utc) - timedelta(days=days)).strftime('%Y-%m-%d %H:%M:%S')
            audit_deleted = 0
            usage_deleted = 0
            snapshots_deleted = 0

            # Bound every transaction so cleanup does not monopolize audit.db.
            for table, time_column in (
                ("api_audit_logs", "request_time"),
                ("llm_usage", "created_at"),
            ):
                while True:
                    with self._get_cursor() as cursor:
                        cursor.execute(
                            f"DELETE FROM {table} WHERE id IN ("
                            f"SELECT id FROM {table} WHERE {time_column} < ? LIMIT 500)",
                            (cutoff_date,),
                        )
                        count = cursor.rowcount
                    if table == "api_audit_logs":
                        audit_deleted += count
                    else:
                        usage_deleted += count
                    if count < 500:
                        break

            # A snapshot belongs to audit history and is also the tombstone
            # that revokes a view capability across an interrupted cache
            # deletion, so its deletion must respect the same retention
            # cutoff as api_audit_logs/llm_usage above -- gated, again, by
            # the task_exists() check below (a task that's still alive is
            # never touched).
            #
            # This used to be two independently-triggered branches:
            #   1. content_expired=1 (properly tombstoned by
            #      cleanup_task_status) with no remaining api_audit_logs
            #      reference -- unbounded by age.
            #   2. archived_at older than cutoff, regardless of
            #      content_expired (H5, local codex review round 7): catches
            #      snapshots that never got properly tombstoned (content_
            #      expired stuck at 0).
            # M1 (local codex review round 10): branch 1 being age-unbounded
            # was a retention bypass -- a task whose audit-log write failed
            # (never referenced in api_audit_logs to begin with, the exact
            # scenario the G1 fix protects) could have its snapshot
            # tombstoned and then deleted via branch 1 within moments of
            # being archived, long before storage.audit_log_retention_days
            # elapses. The fix requires branch 1 to also respect the
            # retention cutoff -- at which point it becomes a strict subset
            # of branch 2 (which already ignores content_expired/ref state
            # and only checks the cutoff), so the two branches collapse into
            # a single age check: content_expired and the api_audit_logs
            # reference no longer gate snapshot deletion at all, only
            # archived_at vs. the retention cutoff does. Keeping the old
            # OR'd-but-now-redundant condition around would just be dead
            # weight that misleads future readers into thinking tombstoned
            # snapshots can still be deleted early.
            snapshot_offset = 0
            while True:
                with self._get_cursor() as cursor:
                    cursor.execute('''
                        SELECT s.task_id
                        FROM task_audit_snapshots s
                        WHERE s.archived_at < ?
                        ORDER BY s.task_id
                        LIMIT 500 OFFSET ?
                    ''', (cutoff_date, snapshot_offset))
                    candidates = [row[0] for row in cursor.fetchall()]
                if not candidates:
                    break
                removable = (
                    [task_id for task_id in candidates if not task_exists(task_id)]
                    if task_exists is not None
                    else []
                )
                if removable:
                    placeholders = ",".join("?" for _ in removable)
                    with self._get_cursor() as cursor:
                        cursor.execute(
                            f"DELETE FROM task_audit_snapshots "
                            f"WHERE task_id IN ({placeholders})",
                            removable,
                        )
                        snapshots_deleted += cursor.rowcount
                snapshot_offset += len(candidates) - len(removable)
                if len(candidates) < 500:
                    break

            deleted_count = audit_deleted + usage_deleted + snapshots_deleted
            logger.info(
                f"清理了 {audit_deleted} 条超过 {days} 天的审计日志记录、"
                f"{usage_deleted} 条超过 {days} 天的 LLM 用量记录、"
                f"{snapshots_deleted} 条无引用任务快照"
            )
            return deleted_count

        except Exception as e:
            logger.error(f"清理审计日志失败: {str(e)}")
            raise


# 全局审计日志记录器实例
_audit_logger = None
_audit_logger_lock = threading.Lock()


def get_audit_logger() -> AuditLogger:
    """
    获取全局审计日志记录器实例（单例模式）

    Returns:
        AuditLogger: 审计日志记录器实例
    """
    global _audit_logger

    if _audit_logger is None:
        with _audit_logger_lock:
            if _audit_logger is None:
                _audit_logger = AuditLogger()

    return _audit_logger
