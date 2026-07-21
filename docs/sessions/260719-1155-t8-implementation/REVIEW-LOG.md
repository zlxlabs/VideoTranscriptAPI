# T8 实施 session ——  review 日志与 backlog

> 创建：2026-07-19
> 任务：T8 阶段二逐段校对推广 v1（确定性段落化），任务卡 `docs/sessions/260719-0513-chapters/TASKS.md`
> 纪律：独立 subagent review ≤20 轮；P1（正确性/安全/数据丢失）必修；P2/P3 可判「接受不修」记本文件 backlog 并写理由；gate = 连续 2 轮无新增 P1；减法优先，新机制仅用于消除 P1。

## Review 轮次记录

| 轮次 | reviewer | 新增 P1 | P2/P3 新增 | 处置 |
|------|----------|---------|------------|------|
| R1（2026-07-19，agent-3） | 独立 coder subagent | 0 | F1(P2) + F2~F7(P3) | 结论 PASS。F1/F7 属完成标准明列项但无测试锁定 → 补测关闭；F2~F6 接受不修（理由见 backlog）。`unknown_id` 键裁决 P3 接受（语义已满足，键名为 FunASR 共享既有 schema，改名违反减法优先）；建议 T10 doc pass 把「序列化全文不含 unknown」改写为「无 speaker 占位 unknown（calibration_stats 既有计数键 unknown_id 除外）」。integration 15 条失败经 6512dac 基线复跑证明全部为既有/环境性，T8 零新增回归。 |
| R1 补测（2026-07-19，agent-4，commit `7d4935f`） | coder subagent | — | — | 仅加测试：F1 `TestNameRestorationNoOpOnPlainStructuredArtifact`（4 例，直调 no-op 返回原对象 + `_save_llm_results` 调用点 spy 零调用）+ F7 `TestRenderNoneTimeParagraphs`（2 例，None 时间段落有 dlg 锚点、无 "00:00:00"/time-tag，混排不误伤正常时间标签）。tests/unit 全绿 exit 0。 |
| R2（2026-07-19，agent-5） | 独立 coder subagent | 0 | N1~N3(P3) | 结论 PASS。N1~N3 接受不修（理由见 backlog）；R1 六项 P3 经独立复核全部认可；`unknown_id` 独立复核维持 P3 裁决。reviewer 另跑 3000 组 paragraphize fuzz 通过；XSS 探测被 html.escape 拦下；tests/unit 2433 passed；integration 10 条失败经基线复跑确认环境性（基线同文件 20 条），T8 零新增回归。**gate 达成：R1、R2 连续两轮无新增 P1，review 循环关闭。** |

## Backlog（接受不修的 P2/P3）

| 日期 | 轮次 | 发现 | 级别 | 接受理由 |
|------|------|------|------|----------|
| 2026-07-19 | R1 | F2 `paragraphize._smart_join` 成员自带前导空白时插空格判定用 `part.lstrip()` 但拼接用原 part，可产双空格（与 docstring 不符） | P3 | 纯外观；真实 segments 极少带前导空白；段落文本不进 fingerprint 输入 |
| 2026-07-19 | R1 | F3 echo 拒绝正则 `^\[\d+\](\[|:)` 加宽后对 speaker 路径以 `[数字]:` 开头的合法校对文本误拒（后果仅该 id 保留原文） | P3 | ASR 文本极少此形态；后果为保守保留原文，不产错误文本 |
| 2026-07-19 | R1 | F4 plain 任务 segments 若混 speaker 标签，processor 自动走 has_speaker=True 但产物仍被打 `plain_structured` 标 | P3 | 现实数据路径不可达（入队点置 None + funasr 行防护 + 侧车无 speaker 字段）；可选加固：打标条件追加 `not structured_data.get("speaker_mapping")` |
| 2026-07-19 | R1 | F5 终端规则实现为「最后一个成员边界」而非字面「词边界硬切」 | P3 | 与「断点只落成员边界」不变式、规格 L165 unit 边界要求、规则 5（单段不切）唯一自洽的解读；单测锁成员不腰斩 |
| 2026-07-19 | R1 | F6 开关反复翻转 + 穿插 calibrate=false/recalibrate 轮时 llm_processed.json 可能相对 llm_calibrated.txt 陈旧 | P3 | 方案 b「只增不减」的已接受取舍；暗启动期可忽略 |
| 2026-07-19 | R1 | `unknown_id` 计数键使「序列化全文不含 unknown」字面不可达 | P3 | 语义意图（无 speaker 占位 unknown）已满足且双测试锁定；键名为 FunASR 共享既有 schema，改名动 FunASR 路径违反减法优先；S6 等价断言方向正确。R2 独立复核维持此裁决 |
| 2026-07-19 | R2 | N1 `_resolve_plain_structured_segments`：dict 形 transcription_data 无可用 "segments" 时直接 return None，跳过缓存侧车回退 | P3 | 现不可达（各入队点对 plain 任务都置 None）；若加固可 fall through 到缓存查询 |
| 2026-07-19 | R2 | N2 单条 >plain_max(4000) 字符 segment 被拆分时子条时间戳复制，snapshot 条数失配后段落化回退消费 HH:MM:SS 截断值，停顿授权降级 | P3 | 病理输入限定；文本完整性不受影响（fuzz 验证覆盖不变式） |
| 2026-07-19 | R2 | N3 侧车 get_cache 读取在 media_lock 外，与并发写者存在撕裂窗口 | P3 | 最坏结果 JSON 损坏 → 返回 None → 本轮诚实降级纯文本，下轮自愈；降级方向安全 |
