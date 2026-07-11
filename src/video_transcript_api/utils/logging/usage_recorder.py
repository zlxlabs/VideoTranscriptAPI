"""LLM token 用量审计记录器

将每次 LLM 调用（`llm/core/llm_client.py::LLMClient.call()`）的 token 用量、
耗时、任务/阶段上下文写入 audit.db 的 `llm_usage` 表（schema v3，参见
`audit_logger.py::AuditLogger._migrate_v3`），用于按任务/阶段做用量审计和
成本分析。

设计要点：
- 复用 `AuditLogger` 的连接池（threading.local + WAL），不新开连接，避免
  多一份 SQLite 文件句柄管理逻辑（DRY）。`api/routes/audit.py` 里已有先例
  直接使用 `audit_logger._get_cursor()`，此处沿用同一约定。
- 录制失败（DB 异常等）绝不能影响 LLM 调用主流程：内部 try/except +
  warning 日志（fail-open），调用方无需关心记录是否成功。
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from .audit_logger import AuditLogger, get_audit_logger
from .logger import setup_logger

logger = setup_logger("usage_recorder")

# get_stats() 返回的空聚合结构，读取异常或无数据时使用
_EMPTY_TOTAL: Dict[str, int] = {
    "call_count": 0,
    "prompt_tokens": 0,
    "completion_tokens": 0,
    "total_tokens": 0,
    "usage_missing_count": 0,
}


class UsageRecorder:
    """LLM token 用量记录器（线程安全，写 audit.db 的 llm_usage 表）"""

    def __init__(self, audit_logger: Optional[AuditLogger] = None):
        """初始化用量记录器

        Args:
            audit_logger: 复用的 AuditLogger 实例，默认使用全局单例
                （与 API 调用审计共用同一个 audit.db 文件）
        """
        self._audit_logger = audit_logger or get_audit_logger()
        # AuditLogger 内部连接按线程隔离已是线程安全的，这里额外加锁是为了
        # 保护 record() 方法本身在同一线程内的可重入调用不产生竞态（防御性）
        self._lock = threading.Lock()

    def record(
        self,
        *,
        task_id: Optional[str],
        stage: Optional[str],
        model: Optional[str],
        prompt_tokens: Optional[int],
        completion_tokens: Optional[int],
        total_tokens: Optional[int],
        duration_ms: int,
        usage_missing: bool,
    ) -> bool:
        """记录一次 LLM 调用的 token 用量

        Args:
            task_id: 任务 ID，缺失时记为 'unknown'
            stage: 调用阶段（calibration/summary/speaker_inference/validation 等），
                缺失时记为 'unknown'
            model: 实际使用的模型名，缺失时记为 'unknown'
            prompt_tokens/completion_tokens/total_tokens: token 用量，provider
                未回报时为 None（此时按 0 落库，但 usage_missing 置位）
            duration_ms: 本次调用耗时（毫秒）
            usage_missing: provider 未回报 usage 时为 True，用于审计识别
                "有调用但拿不到用量" 的情况，避免和 "确实消耗 0 token" 混淆

        Returns:
            bool: 是否记录成功；写库异常时返回 False 但不抛出，不影响 LLM
                调用主流程（fail-open）
        """
        try:
            resolved_task_id = task_id or "unknown"
            resolved_stage = stage or "unknown"
            resolved_model = model or "unknown"
            created_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

            with self._lock:
                with self._audit_logger._get_cursor() as cursor:
                    cursor.execute(
                        """
                        INSERT INTO llm_usage
                        (task_id, stage, model, prompt_tokens, completion_tokens,
                         total_tokens, duration_ms, usage_missing, created_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            resolved_task_id,
                            resolved_stage,
                            resolved_model,
                            prompt_tokens or 0,
                            completion_tokens or 0,
                            total_tokens or 0,
                            duration_ms,
                            1 if usage_missing else 0,
                            created_at,
                        ),
                    )
            return True
        except Exception as exc:
            logger.warning(f"LLM usage 记录失败（不影响 LLM 调用主流程）: {exc}")
            return False

    def get_stats(self, days: int = 30) -> Dict[str, Any]:
        """按 stage 聚合 token 用量统计，供 /api/audit/stats 使用

        Args:
            days: 统计天数窗口，默认 30 天

        Returns:
            dict: {
                "by_stage": [{"stage", "call_count", "prompt_tokens",
                               "completion_tokens", "total_tokens",
                               "usage_missing_count"}, ...],
                "total": {同上字段的汇总},
                "days": days,
            }
            查询异常时返回全 0 的空结构，不抛出。
        """
        try:
            cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime(
                "%Y-%m-%d %H:%M:%S"
            )

            with self._audit_logger._get_cursor() as cursor:
                cursor.execute(
                    """
                    SELECT stage,
                           COUNT(*) as call_count,
                           SUM(prompt_tokens) as prompt_tokens,
                           SUM(completion_tokens) as completion_tokens,
                           SUM(total_tokens) as total_tokens,
                           SUM(usage_missing) as usage_missing_count
                    FROM llm_usage
                    WHERE created_at >= ?
                    GROUP BY stage
                    ORDER BY total_tokens DESC
                    """,
                    (cutoff,),
                )
                rows = cursor.fetchall()

            by_stage = [
                {
                    "stage": row[0],
                    "call_count": row[1] or 0,
                    "prompt_tokens": row[2] or 0,
                    "completion_tokens": row[3] or 0,
                    "total_tokens": row[4] or 0,
                    "usage_missing_count": row[5] or 0,
                }
                for row in rows
            ]

            total = dict(_EMPTY_TOTAL)
            for stage_row in by_stage:
                for key in total:
                    total[key] += stage_row[key]

            return {"by_stage": by_stage, "total": total, "days": days}
        except Exception as exc:
            logger.error(f"LLM usage 统计查询失败: {exc}")
            return {"by_stage": [], "total": dict(_EMPTY_TOTAL), "days": days}


_usage_recorder: Optional[UsageRecorder] = None
_usage_recorder_lock = threading.Lock()


def get_usage_recorder() -> UsageRecorder:
    """获取全局 UsageRecorder 单例（复用全局 AuditLogger 连接池）

    Returns:
        UsageRecorder: 用量记录器实例
    """
    global _usage_recorder

    if _usage_recorder is None:
        with _usage_recorder_lock:
            if _usage_recorder is None:
                _usage_recorder = UsageRecorder()

    return _usage_recorder
