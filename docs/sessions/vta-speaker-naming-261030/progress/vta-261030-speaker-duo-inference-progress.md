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
