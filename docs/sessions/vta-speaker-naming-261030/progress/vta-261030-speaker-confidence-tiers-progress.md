# 进度：说话人推测名按证据分高/中/低三档（vta-261030-speaker-confidence-tiers）

> 本卡执行器 dlg-20261030-141106-67ca32。追加式记录；每段四项：当前阶段、本段结论、
> 关键决策与已否决方案、下一步唯一动作。补充说明：本卡三个里程碑各自 commit 时
> 未同步追加进度段（进度文件在本卡收尾时一次性补写），偏离了卡面「与该里程碑的
> commit 同一次提交」条款，已在 report.md「与卡面的偏差」里登记。

## 2026-10-30（caca2dbd 档位字段接入）

- **当前阶段**：implementing（里程碑 1/3 已提交）。
- **本段结论**：新建 `speaker_confidence_level.py`，`infer()` 的 LLM 成功路径在两人通路之后、`_identity_fallback` 出口统一调用 `assign_confidence_levels()`，meta 每条目补 `confidence_level` / `guess_name` / `basis`（quote 截 80 字符，none 档 basis 四键全 null）。单元级 12 例（high 复合名取 dialog 名、medium 按角色取候选、low 带第一条候选与 rejection_reason、五种占位名判 none、not_sampled 丢弃 reason、LLM 失败 fallback 全 none）全绿。
- **关键决策与已否决方案**：identity fallback 不新增字段常量，直接复用 `assign_confidence_levels`（name=原始标签自然落 none），避免第二份键名硬编码；medium 的 guess_name 按卡面语义取显示名冒号后一段（`rsplit(":", 1)`），不从 verified_candidates 重现 pick。证据：`uv run --frozen pytest tests/unit/test_speaker_confidence_level.py` → 12 passed, 5 subtests passed（本机 2026-10-30）。
- **下一步唯一动作**：实现 duo 单边命名与 @ 规则收窄，让轴表第一行端到端转绿。

## 2026-10-30（09f4f28a duo 单边命名与 @ 收窄）

- **当前阶段**：implementing（里程碑 2/3 已提交）。
- **本段结论**：`plan_duo_names` 重构为「先收集两位候选与唯一名 → 双边（名字不同）走现行 pick；单边（另一位零静态候选）要求来源与角色一致（author_host 只配 host、title_credit 只配 guest）」；@ 闸从「标题 @ 总数 >1」收窄为「仅当实际采用的 title_credit 候选名字来自账号（quote 里 `@名字` 形式）时，标题其余 @ 提及 ≥1 才关闭」。轴表第一行端到端（S2 扩写全名判 low + S1 单边 medium）、Ali 全名标题双 medium、单边五例、账号型 @ 关闭两例全绿。
- **关键决策与已否决方案**：轴表未覆盖格「另一位挂两个不同静态名」——否决了「整条关闭」，按「总能给出最可能的人名」目标处理：solo 一位照常定名、另一位保持占位（其定名正确性不依赖另一位候选），测试 docstring 与报告均已登记。测试新增红验：注入改坏 `_role_consistent_candidate`（忽略角色一致）→ 2 例 AssertionError 红 → 还原；注入改坏 `_is_placeholder_name`（恒 False）→ 7 例 AssertionError 红 → 还原。证据：`pytest tests/unit/test_speaker_duo_inference.py tests/unit/test_speaker_confidence_level.py` → 37 passed, 14 subtests passed（本机 2026-10-30）。
- **下一步唯一动作**：改提示词两行（照抄原样写法）+ 规则版本升 8，跑 make test 收尾。

## 2026-10-30（52f69004 提示词与版本号）

- **当前阶段**：implementing（里程碑 3/3 已提交，收尾中）。
- **本段结论**：提示词 `title_credit` / `author_host` 两行原位改写（行数净 0）：name 必须照抄引用原样写法（`@sama` 填 `sama`）、作者名取字段原文人名部分（「金明WilliamJoy」取「金明」），删「仅当另有 dialog_address 佐证」改为「仅凭标题/作者名定名属于推测，采用时以『标题/作者关联推测』标注」；`test_prompt_selection.py` 锁定替换文本同步；`SPEAKER_EVIDENCE_RULES_VERSION` 7→8（旧缓存自然失效）。`make test` EXIT=0（进度条 [100%] 全点号），`compileall` 退出码 0，`card_scope_check.py` 8 个变更文件均匹配 Scope-Globs。
- **关键决策与已否决方案**：零余量文件（speaker_inferencer.py / prompts/__init__.py / test_speaker_inferencer.py）行数闸以 `git diff --numstat base..HEAD` 逐次核对，全部净 0（版本注释多出的行用压缩既有注释抵消）；总 diff 净 788 行超 target 500 未破 hard 1000，主因是轴表要求的两个新测试文件，不裁测试。证据：本机 make test 日志 /tmp/make_test2.log（2026-10-30）。
- **下一步唯一动作**：push 分支、写 report.md 收尾。
