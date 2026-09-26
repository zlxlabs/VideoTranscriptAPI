"""性能埋点模块

提供上下文管理器记录各阶段耗时，支持缓存命中/未命中计数和统计汇总。
"""

import threading
import time
from contextlib import contextmanager
from typing import Dict, Optional, List
from .logging import setup_logger

logger = setup_logger("perf_tracker")


class PerfTracker:
    """性能追踪器

    使用上下文管理器记录各阶段耗时，线程安全。

    Usage:
        tracker = PerfTracker(task_id="task-001")
        with tracker.track("download"):
            download_file(...)
        with tracker.track("transcribe"):
            transcribe(...)
        print(tracker.summary())
    """

    def __init__(self, task_id: str = ""):
        """初始化性能追踪器

        Args:
            task_id: 关联的任务 ID（用于日志标识）
        """
        self.task_id = task_id
        self._records: List[Dict] = []
        self._counters: Dict[str, int] = {}
        self._lock = threading.Lock()

    @contextmanager
    def track(self, stage: str):
        """记录指定阶段的耗时

        Args:
            stage: 阶段名称（如 "download", "transcribe", "llm_calibrate"）

        Yields:
            None
        """
        start = time.monotonic()
        error = None
        try:
            yield
        except Exception as e:
            error = e
            raise
        finally:
            elapsed_ms = (time.monotonic() - start) * 1000
            record = {
                "stage": stage,
                "elapsed_ms": round(elapsed_ms, 2),
                "success": error is None,
            }
            with self._lock:
                self._records.append(record)

            status = "OK" if error is None else f"FAILED: {error}"
            logger.info(
                f"[perf] {self.task_id} | {stage}: {elapsed_ms:.0f}ms ({status})"
            )

    def count(self, name: str, delta: int = 1):
        """递增一个计数器

        Args:
            name: 计数器名称（如 "cache_hit", "cache_miss"）
            delta: 增量
        """
        with self._lock:
            self._counters[name] = self._counters.get(name, 0) + delta

    def get_elapsed(self, stage: str) -> Optional[float]:
        """获取指定阶段的耗时（毫秒）

        Args:
            stage: 阶段名称

        Returns:
            float: 耗时（毫秒），未找到返回 None
        """
        with self._lock:
            for record in reversed(self._records):
                if record["stage"] == stage:
                    return record["elapsed_ms"]
        return None

    def summary(self) -> Dict:
        """汇总所有阶段的性能数据

        Returns:
            dict: 性能摘要，包含各阶段耗时和计数器
        """
        with self._lock:
            total_ms = sum(r["elapsed_ms"] for r in self._records)
            stages = {}
            for r in self._records:
                stage = r["stage"]
                if stage not in stages:
                    stages[stage] = {
                        "elapsed_ms": 0,
                        "count": 0,
                        "successes": 0,
                        "failures": 0,
                    }
                stages[stage]["elapsed_ms"] += r["elapsed_ms"]
                stages[stage]["count"] += 1
                if r["success"]:
                    stages[stage]["successes"] += 1
                else:
                    stages[stage]["failures"] += 1

            return {
                "task_id": self.task_id,
                "total_ms": round(total_ms, 2),
                "stages": stages,
                "counters": dict(self._counters),
            }

    def observation(self) -> Dict:
        """Return completed intervals and the cache counters used by reporting."""
        summary = self.summary()
        counters = {
            name: value
            for name, value in summary["counters"].items()
            if name in {"cache_hit", "cache_hit_partial"}
            and isinstance(value, int)
            and not isinstance(value, bool)
            and value >= 0
        }
        observation = {"stages": summary["stages"]}
        if counters:
            observation["counters"] = counters
        return observation

    def log_summary(self):
        """将性能摘要输出到日志"""
        s = self.summary()
        parts = [f"[perf-summary] {self.task_id} | total: {s['total_ms']:.0f}ms"]
        for stage, data in s["stages"].items():
            parts.append(f"  {stage}: {data['elapsed_ms']:.0f}ms (x{data['count']})")
        for name, value in s["counters"].items():
            parts.append(f"  {name}: {value}")
        logger.info("\n".join(parts))
