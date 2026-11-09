## 里程碑 1：输出语言校验实现

- 当前阶段：实现已落盘，尚未验证。
- 本段结论：总结的三个成功出口都经过语言校验，最终文本不匹配时返回 FAILED 并记录 `SUMMARY-LANGUAGE-MISMATCH`。笔记汇总文本也在 GENERATED 前使用同一 detector 校验，并以 `NOTES-LANGUAGE-MISMATCH` 返回既有 FAILED 状态。
- 关键决策与已否决方案：直接复用 `detect_language` 与现有占比阈值；目标语言由 `get_language()` 映射到 `en` / `zh`；保留总结、笔记已有 prompt，不新增公共抽象或重试。
- 下一步唯一动作：写入报告主体并提交实现，然后执行新增测试及卡面验证命令。

## 里程碑 2：首轮验证发现测试夹具语言假设

- 当前阶段：实现已提交；首轮 Accept-Check 通过，Narrow-Verify 发现既有 mock 响应与目标语言不一致。
- 本段结论：总结处理器自身测试中多条预算断言受测试夹具全局 mock prompt 语言影响，英语响应应通过消费者读取的目标语言显式置为英语，但不应改变原有预算系数。Coordinator 与 share-card 集成测试的英文 mock 也需要显式请求英语；生产校验保持 fail-closed，不为兼容旧夹具放宽。
- 关键决策与已否决方案：不移除语言校验，也不把检测降级为日志；只修正测试 fixture 的请求语言与模拟响应语义，保留预算回归的原系数。
- 下一步唯一动作：提交测试夹具修正并重新运行 Accept-Check 与 Narrow-Verify。
