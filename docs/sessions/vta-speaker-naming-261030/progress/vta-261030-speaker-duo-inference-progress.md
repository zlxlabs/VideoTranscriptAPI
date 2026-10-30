# 两人访谈「元数据 + 角色」推测通路进度

## 里程碑 1：通路骨架与结构前提判定（2026-10-30）
- 当前阶段：implementing，生产代码与机械测试改动已落盘，窄范围测试已通过。
- 本段结论：新增 `llm/core/speaker_duo_inference.py`（主要说话人判定 + 结构前提核对 +
  显示名裁决）与 `llm/prompts/speaker_duo_roles.py`（roles 提示词片段）；说话人映射
  schema 新增必填 `roles` 字段；`infer()` 在证据闸门之后调用通路，规则版本 6 → 7。
- 关键决策与已否决方案：提示词段落**不进** `prompts/__init__.py`——该文件有提示词一致性
  锁（`tests/unit/i18n_llm/test_prompt_selection.py` 按固定 base SHA 逐字比对），改动它
  需要扩 Scope（该测试文件不在本卡 Scope-Globs 内）且会打破锁；改为在 `infer()` 的
  system prompt 拼接口处引用 `speaker_duo_roles.py`，提示词正文仍归 prompts 包。
  `SPEAKER_MAPPING_SCHEMA_VERSION` 保持 2：缓存信封契约未变，读侧在
  `cache/cache_manager.py`（不在本卡 Scope 内），规则版本进指纹已让旧缓存自然失效。
- 下一步唯一动作：用真实夹具写轴表第一行的端到端失败测试，再补齐其余轴表格子。

## 里程碑 2：真实夹具与轴表测试（2026-10-30）
- 当前阶段：implementing，轴表测试与真实夹具已落盘，窄范围与全量回归均通过。
- 本段结论：真实夹具（twitter 2115809881399922849，时间轴前 40 段，两人各 20 段）
  从 `SpeakerInferencer.infer()` 入口跑到渲染产物，两位说话人拿到
  `说话人1｜作者关联推测:John Collison` 与 `说话人2｜标题关联推测:sama`，渲染 HTML
  不含 `data-speaker-status="pending"`；轴表其余格子（角色 unknown、角色重复、3 人
  分区、3% 小簇、同名、两个 @ 提及、撞名、缺 roles 字段）各有对应用例。
- 关键决策与已否决方案：撞名判据按锁定决策的 casefold 字面口径实现，不引入
  前缀/子串启发式（程序不做「Sam 与 sama 是同一人」这类语义猜测）；轴表第八格
  的期望由「两位主要说话人各至少 1 条静态候选」这条前提满足（见 report.md
  「与卡面的偏差」）。真实夹具按公开已发布内容入仓，裁剪方式写在夹具 README。
- 下一步唯一动作：跑红验（改坏判据确认断言会红）、全量回归与 scope 自检，写执行器报告。
