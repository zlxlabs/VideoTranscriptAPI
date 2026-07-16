"""校对 / 总结 "诚实状态模型" 常量.

背景:
    校对有两条路径(纯文本 PlainTextProcessor / 说话人结构化 SpeakerAwareProcessor),
    过去只有结构化路径产出质量统计,纯文本路径的降级完全不可见;
    总结失败与"文本过短跳过"共用同一个 None 返回值,导致失败被误判为正常跳过,
    进而在 cache_manager 里被当成"文件缺失=处理中"，在前端永久显示"总结处理中...".

    本模块集中定义这两类状态的取值,作为 processor -> coordinator -> llm_ops
    -> cache_manager -> 前端 这条链路上的统一契约,避免裸字符串散落各处。
    风格与 utils/task_status.py 的 TaskStatus 保持一致(StrEnum,可直接当字符串用)。

放置位置:
    与 task_status.py 同级(utils 包),而不是 llm 包内 —— 这样 cache 包(cache_manager.py)
    和 llm 包都能引用而不产生 llm <-> cache 的循环依赖(cache_manager.py 已经在用
    utils/task_status.py 的先例)。
"""

from enum import StrEnum


class CalibrationStatus(StrEnum):
    """校对状态: 描述一次校对任务里,内容多大程度上真正经过 LLM 校对成功。

    两条校对路径(纯文本按分段 segment、结构化按分块 chunk)统计口径不同,
    但都收敛到这三个取值,方便下游(通知、落盘、前端警告条)统一消费。
    """

    FULL = "full"        # 全部内容成功由 LLM 校对，没有任何原文兜底
    PARTIAL = "partial"  # 部分内容降级为原文或低质量输出，部分正常
    NONE = "none"         # 全部内容降级为原文（LLM 校对完全失败）
    DISABLED = "disabled"  # 用户通过 processing_options.calibrate=False 主动关闭校对
                            # （区别于 NONE：NONE 是"尝试了但失败"，DISABLED 是"根本没尝试"）


class SummaryStatus(StrEnum):
    """总结状态: 区分"未触发生成"(正常路径)和"触发了但失败"，避免二义 None。

    历史 bug: SummaryProcessor.process() 失败也返回 None，协调器判断文本过短
    也返回 None，两种 None 在下游完全无法区分，最终表现为"总结处理中..."永久占位。
    """

    GENERATED = "generated"          # 总结成功生成
    SKIPPED_SHORT = "skipped_short"  # 原文过短，未触发总结生成（正常路径，非失败）
    FAILED = "failed"                # 触发了生成但失败（LLM 异常或输出过短/为空）
    PENDING = "pending"              # 总结阶段尚未执行完成（任务仍在处理中）
    DISABLED = "disabled"            # 用户通过 processing_options.summarize=False 主动关闭总结
                                      # （区别于 SKIPPED_SHORT：SKIPPED_SHORT 是"想生成但文本太短"，
                                      # DISABLED 是"用户压根不想要总结"）


class ChaptersStatus(StrEnum):
    """章节梗概状态：与 SummaryStatus 并列的"诚实状态模型"，语义一一对应。

    章节生成比总结多一种正常跳过路径：SKIPPED_NO_TIMELINE ——
    输入根本没有可用的时间轴信息（segments 为 None/空），既不算"过短"也不算"失败"，
    是从一开始就不具备生成条件的正常路径。这一路径同样覆盖"过滤后有效 segment
    数少于 2 个"的情形（哪怕单个 segment 的文本长度已超过 min_chapters_threshold）：
    单块时间轴没有任何内部边界可供切分，结构上不可能产出章节功能要求的至少 2
    个章节，对导航毫无锚定价值，语义上等同于"没有可用的时间轴"——必须在调用
    LLM 之前就判定为 SKIPPED_NO_TIMELINE，而不是让 LLM 调用注定因步骤 3 的章节
    数量下限校验失败（FAILED 在分层补跑语义里是"可重试"，会导致这种结构上永远
    不可能成功的输入被反复重试、反复消耗 LLM 调用）。
    """

    GENERATED = "generated"                    # 章节成功生成
    SKIPPED_SHORT = "skipped_short"             # 原文过短，未触发章节生成（正常路径，非失败）
    SKIPPED_NO_TIMELINE = "skipped_no_timeline"  # 没有可用的分段时间轴，无法生成章节（正常路径，非失败）；
                                                  # 含"有效 segment 数 < 2"这种结构上不可能分章的情形
    FAILED = "failed"                           # 触发了生成但失败（LLM 异常、输出不合法或结构校验不通过）
    PENDING = "pending"                         # 章节阶段尚未执行完成（任务仍在处理中）
    DISABLED = "disabled"                       # 用户主动关闭章节生成
                                                 # （区别于 SKIPPED_SHORT/SKIPPED_NO_TIMELINE：
                                                 # 那两者是"想生成但条件不满足"，DISABLED 是"用户压根不想要"）
