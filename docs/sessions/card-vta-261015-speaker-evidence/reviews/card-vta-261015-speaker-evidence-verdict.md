failure-visibility: p2-only

# 说话人证据裁决实现审查结论
## 总 verdict

**pass-with-backlog**。冻结范围 `95d21bb5..7e38dd73` 未发现 P1；证据裁决、confidence 解耦、显示名规则、schema v2 与三个指纹调用点均基本按 spec 落地。以下 P2 可接受不修、不阻断交付。

## Findings

1. **metadata override 使缓存读写指纹不一致** — `src/video_transcript_api/api/services/transcription.py:1413`、`src/video_transcript_api/api/services/llm_ops.py:1840`；违反 **spec 4**；**P2，不阻断**。两个读侧都从旧缓存元数据算指纹；`transcription.py` 随后才在 `:1734` 调用 `finalize_presentation_metadata()`，新推断写侧使用 finalize 后的标题、作者、描述。override 改变这些字段时，缓存预检会 miss，identity-fallback 姓名恢复也可能跳过，导致额外推断或保留占位名。触发依赖非默认 override，后果可重算、可恢复。
2. **top2 配额覆盖了配置限额和零值** — `src/video_transcript_api/llm/core/speaker_inferencer.py:576`；违反 **spec 6**；**P2，不阻断**。top2 总是使用 8/1200，即使 `samples_per_speaker` 或 `max_chars_per_speaker` 配成更大值或 0。卡面注明生产未配置这两项，仓内示例默认值为 3/400；风险限于显式调整配置时。建议后续修复，保留更大限额并尊重 0 的停用语义；改动小，约数行，但不构成本轮交付阻断。
3. **候选作废的具体原因未完整写入 meta** — `src/video_transcript_api/llm/core/speaker_inferencer.py:1182`；违反 **spec 1、9**；**P2，不阻断**。`_verify_candidates()` 返回的 `invalid_candidate`、`unknown_evidence_type` 等原因只在“所有候选均 quote_missing”时被使用；混合拒绝（如伪造 quote 加域外类型）会丢掉 `quote_missing`，meta 仅写 `no_verified_evidence` 或裁决拒绝原因。结果仍 fail-closed，warning 可见拒绝，但 meta 没有保存具体作废原因，也违背代码注释要求 quote_missing 可观测。
4. **v1/v2 分叉与指纹 miss 缺少可区分日志** — `src/video_transcript_api/llm/core/speaker_inferencer.py:370`、`src/video_transcript_api/cache/cache_manager.py:1655`；违反 **spec 9**；**P2，不阻断**。缓存命中仅记录通用 “Retrieved speaker_mapping” 后静默进入 v1 或 v2 分支；两套 `get_speaker_mapping()` 对普通指纹不匹配均直接返回 `None`。因此预检触发重新推断时，日志不能区分指纹 miss 与缓存缺失/其他 miss。`llm_ops.py` 的姓名恢复路径单独有 mismatch 日志，但不能覆盖另外两条读路径。
5. **v2 候选 meta 的真实文件产物未被契约测试锁定** — `tests/unit/test_speaker_artifact.py:36`、`tests/unit/test_speaker_evidence_gate.py:225`；违反 **spec 5、7**。持久化测试写入的 result 没有 `candidates`，只断言 envelope `schema_version=2`；v2 缓存重放测试使用 `MagicMock` 返回对象，没有穿过 JSON 写入/读取边界。当前写入代码会序列化完整 result，但缺少对真实 producer 文件内容的断言，无法锁住 `candidates` 等字段落盘后仍能触发 v2 直用分支。**P2，不阻断**。
6. **证据类型没有绑定引用所属分区** — `src/video_transcript_api/llm/core/speaker_inferencer.py:1079`、`:1082`；违反 **spec 2**（并暴露 spec 1 约束不足）；**P2，不阻断**。程序只验证 `evidence_type` 是域值、quote 是整个 `user_prompt` 的子串，没有验证 `dialog_address` 引用来自转录区。若 LLM 将仅出现在 `description` 的名单姓名错误标为 `dialog_address`，单条该候选就会被采用；因此“description_roster 永不独立生效”依赖 LLM 不误标，未由程序保证。当前没有真实模型调用证据证明本轮触发，按可排查/可修复的 P2 记账；需要把来源与类型绑定，或收窄 spec 2 的保证范围。
7. **top2 排名在每个说话人循环内重复计算** — `src/video_transcript_api/llm/core/speaker_inferencer.py:576`、`:631`；违反 **spec 8**；**P2，不阻断**。单消费者 helper `_top_sample_speakers()` 每次调用都会重新排序全部 speaker；它在 `for speaker in speakers` 内调用，形成 N 次排序（发言量相同时整体为 O(N²)）。该 helper 没有第二个调用方，且 `speakers` 参数未使用；可在循环前计算一次 top2 集合并移除不必要参数，避免重复计算及单消费者封装。常见小规模影响有限。

8. **旧 v1 工件无法到达 confidence-gate 重判分支** — `src/video_transcript_api/llm/core/speaker_inferencer.py:361`、`src/video_transcript_api/cache/cache_manager.py:1669`；违反 **spec 5**；**P2，不阻断**。指纹算法加入元数据和 rules_version 后，所有旧 v1 工件仍携带旧算法哈希；缓存读侧在形状分叉前要求新指纹精确相等，因此历史 v1 工件会直接 miss 并重算，不会进入旧 gate。现有 v1 测试用 `MagicMock` 直接返回工件，绕过真实读侧校验。重算安全且可恢复，但“v1 命中走旧 gate”没有被生产路径实现；若不打算复用旧工件，应明确这是预期失效而非兼容命中。

## 预登记 findings 复核

- **F1/F2：确认，P2。** 两处读侧使用缓存快照，写侧使用 finalize 后元数据；仅在 metadata override 改变字段时触发。后果是 cache miss/额外计算或姓名恢复跳过，可恢复，不阻断。
- **F3：确认，P2。** top2 常量无条件覆盖配置，包括 0；卡面注明生产未配置这两项，仓内示例默认值为 3/400。约数行可修，建议后续修复以保留操作员设置；本轮仍不阻断。

## 阻断判断与理由

没有 P1，也没有任何门禁级阻断项。P2 严重度与交付阻断分开裁决：以上均允许记 backlog，故本次审查结论为 **pass-with-backlog**。静默失败维度存在 P2 可排查性与 meta 诊断缺口，故机读值为 `p2-only`；未发现吞错或静默采用无证据姓名的 P1。

## 熵增复核

- `SPEAKER_EVIDENCE_TYPES` 有 schema 与运行时核对两个消费者；`SPEAKER_MAPPING_SCHEMA_VERSION` 被两套缓存读写器消费。
- `_apply_evidence_gate` 与 v1 confidence gate 并存是 spec 5 兼容路径所需；候选核对和名称裁决分成独立步骤，分别有直接单元测试覆盖。
- `_top_sample_speakers` 为单调用方且循环内重复排序，列为 Finding 7。
- 新增 meta 字段服务候选审计、通过证据审计、作废诊断；rules_version 用于指纹失效缓存。未发现其他无消费者状态或配置项。

## 审查范围与验证

- 仅审 `95d21bb5584862fc84b515848e7c1a642be480c7..7e38dd737b573a46b87214e8806d0dbf972709b8`；fixture JSON 按卡面契约排除。
- 未修改 `src/`、`tests/`；本次不重跑测试。派发卡提供的验证记录为 Narrow-Verify 59 passed，以及全量 3778 passed / 1 inherited AF_UNIX path-too-long failure。
- OCR 初筛状态为 `reviewed_fallback`（DeepSeek）；完整捕获的 envelope 有 5 条候选、5 条确认。第一次终端展示被截断，重扫后落盘读取全量 JSON；OCR 只作候选，以上 findings 均按源码与测试独立核实。
