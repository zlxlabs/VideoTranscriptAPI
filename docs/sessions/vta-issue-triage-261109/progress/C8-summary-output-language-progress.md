## 里程碑 1：输出语言校验实现

- 当前阶段：实现已落盘，尚未验证。
- 本段结论：总结的三个成功出口都经过语言校验，最终文本不匹配时返回 FAILED 并记录 `SUMMARY-LANGUAGE-MISMATCH`。笔记汇总文本也在 GENERATED 前使用同一 detector 校验，并以 `NOTES-LANGUAGE-MISMATCH` 返回既有 FAILED 状态。
- 关键决策与已否决方案：直接复用 `detect_language` 与现有占比阈值；目标语言由 `get_language()` 映射到 `en` / `zh`；保留总结、笔记已有 prompt，不新增公共抽象或重试。
- 下一步唯一动作：写入报告主体并提交实现，然后执行新增测试及卡面验证命令。
