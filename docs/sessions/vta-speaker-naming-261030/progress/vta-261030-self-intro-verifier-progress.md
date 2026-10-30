# 本人样本自我介绍独立复核进度

## 里程碑 1：规则改独立复核并完成窄测（2026-10-30）
- 当前阶段：implementing，窄范围测试已通过。
- 本段结论：删除了 `name_not_self_intro` 的正则句式清单；只出现在本人发言样本里的 dialog_address 改为每次 `infer()` 最多一次独立 LLM 复核。Prompt 与 schema 放在新文件，未增加 `prompts/__init__.py` 行数。
- 关键决策与已否决方案：复核请求只含 `{id, name, quote}`；缺 id 或域外分类记 `unclear` 并判 `name_not_self_intro`，调用异常沿用 `infer()` 现有路径。不在首轮推断里让模型自报 address_kind。
- 下一步唯一动作：跑 `make test`、编译检查与 scope 检查。
