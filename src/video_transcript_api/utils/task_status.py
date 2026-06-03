"""任务状态常量与状态机辅助.

集中定义任务生命周期状态,避免裸字符串散落各处导致拼写错误静默坏掉状态机。
放在 utils 下而非 api/cache,以便 api 路由与 cache_manager 都能引用而不产生循环依赖。

状态机:
    queued ──► processing ──► calibrating ──► success
                    │              │
                    └──► failed ◄──┘
    (success / failed 为终态,具备黏性,除 recalibrate 显式重置外不被覆写)

HTTP 映射:
    queued / processing / calibrating → 202 (处理中,继续轮询)
    success                           → 200 (完成)
    failed                            → 500 (失败)
    未知/缺失                          → 200 (兜底,与历史行为一致)
"""

from enum import StrEnum


class TaskStatus(StrEnum):
    """任务状态枚举.

    继承 StrEnum (Python 3.11+),成员即字符串本身:
    - ``TaskStatus.SUCCESS == "success"`` 为 True
    - ``str(TaskStatus.SUCCESS) == "success"``,写入 SQLite 时存的是裸值
    因此与历史的裸字符串读写完全向后兼容。
    """

    QUEUED = "queued"
    PROCESSING = "processing"
    CALIBRATING = "calibrating"
    SUCCESS = "success"
    FAILED = "failed"


# 终态:任务生命周期结束,不应再被非显式流程覆写
TERMINAL_STATUSES = frozenset({TaskStatus.SUCCESS, TaskStatus.FAILED})

# 非终态:仍在处理中,崩溃后可被启动恢复扫描标记为 failed
NON_TERMINAL_STATUSES = frozenset(
    {TaskStatus.QUEUED, TaskStatus.PROCESSING, TaskStatus.CALIBRATING}
)


def http_code_for_status(status: str) -> int:
    """根据任务状态返回对外暴露的 HTTP 状态码.

    Args:
        status: 任务状态字符串(或 TaskStatus 成员)

    Returns:
        202(处理中) / 200(完成或未知兜底) / 500(失败)
    """
    if status in NON_TERMINAL_STATUSES:
        return 202
    if status == TaskStatus.FAILED:
        return 500
    # success 与未知状态均兜底为 200,保持历史行为
    return 200
