# T8 实施 session —— 交接（HANDOFF）

> 创建：2026-07-19（第二个实施 session 开头）
> 任务：T8 阶段二逐段校对推广 v1（确定性段落化），规格唯一来源：`docs/sessions/260719-0513-chapters/TASKS.md` 的 T8 节
> 分支：`feat/chapters-wiring`（本地，未 push）

## 前情：上一个 session 为什么中断

第一个实施 session 主循环累积约 60 万 token（消息体 2.1MB），超过 provider 2MB
硬上限；compaction 请求本身也超限失败，session 死锁报废（导出留档：
仓库根目录 `kimi-export-session_-20260719-161035.md`，仅供参考，不要整份喂回上下文）。
本交接文件 + git 提交历史是恢复的完整依据。

**教训（本 session 起执行）**：
- 大件探查/实现一律走 subagent，主循环只收结论，不整读几千行文件；
- 进度随时落盘到本目录；消息体接近上限前主动收工换新 session。

## 已完成（全部已提交，git log 为准）

| 步 | commit | 内容 |
|----|--------|------|
| 方案修订 | `13b00cb` | T8 方案 v1 确定性段落化，增量 review 收敛 |
| S1 | `62aca84` | 确定性段落化工具 paragraphize（长度预算+授权断点），`transcriber/paragraphize.py` |
| S2 | `812ef97` | `structured_calibration_for_plain` 开关 + 段落化/分块配置字段 |
| S5 | `0a55f09` | 渲染器无 speaker 防御修复 + plain_structured 开关回退门控 |
| S3 | `822bb1b` | SpeakerAwareProcessor has_speaker 模式（无说话人逐段校对，零 SpeakerInferencer 调用，落盘前段落化）+ 19 个单测 |

S3 提交时 `uv run pytest tests/unit` 全绿（exit 0，约 2400 项；warnings 摘要会
盖住通过数行，以 exit code 为准）。

## 待办（按序）

> **2026-07-19 收工**：**T8 实施全部完成，review 循环关闭（gate 达成）。**
> S4=`1126d4e`，S6=`6ca788a`，R1 补测=`7d4935f`。
> Review：R1（PASS，0 P1，F1/F7 补测关闭、5 项 P3 接受）→ R2（PASS，0 新增 P1，
> N1~N3 三项 P3 接受）——连续两轮无新增 P1，gate 满足，详见 REVIEW-LOG.md。
> `unknown_id` 键两轮独立裁决均为 P3 接受不修；措辞修订建议移交 T10 doc pass。
> tests/unit 全绿（exit 0）；tests/integration 的失败经 6512dac 基线复跑证明全部
> 为既有/环境性（layered_cache + youtube_priority），T8 零新增回归。
> **后续（非本 session 范围）**：T9 真实样本验收通过后再翻开关默认 true，并决定是否
> 启动 v2 语义段落化；观测项（chunk 数/墙钟/token 对比）留待 T9 记录。
>
> <details><summary>S4/S6 实现要点（归档）</summary>
>
> S4（commit `1126d4e`，`uv run pytest tests/unit` exit 0）：
> 实现要点：`_prepare_llm_content` 保持 3 参签名，内部推导开关+calibrate；
> segments 来源梯度 = transcription_data → 缓存侧车（`get_cache(use_speaker_recognition=False)`，
> 命中 funasr 行防护返回 None）；`plain_structured_active = 非 speaker + content 为 list`
> 透传 `_save_llm_results`；落盘 gate 放宽 + 顶层打 `"mode": "plain_structured"`（双条件防误标）；
> 姓名恢复两块维持 speaker 门控（plain 产物天然 no-op）。新增 16 个单测于
> tests/unit/test_llm_ops_helpers.py。
>
> S6（commit `6ca788a`）：新增
> tests/integration/test_t8_plain_structured_chain.py（3 用例：开关开全链路 /
> 开关关回归 / 无 segments 诚实降级），新文件 3 passed；tests/unit exit 0；
> tests/integration exit 1 但仅 test_youtube_transcript_priority.py 5 条无关既有
> 失败（排除新文件后复现，环境相关，未修）。`calibration_stats` 的 `"unknown_id"`
> 键问题 → 已经 review 两轮裁决为 P3 接受不修（见 REVIEW-LOG.md）。
>
> </details>

1. ~~**S4 — llm_ops 接线**~~（已完成 `1126d4e`） 原描述：（`api/services/llm_ops.py`）：
   - `_prepare_llm_content`：**仅 `calibrate_requested=True`**（含 recalibrate）且
     plain 源有 segments → 返回 list（协调器 isinstance 路由自然生效）；
     calibrate=false 补层任务维持纯文本（防指纹永久 mismatch → 永久 nolink）；
   - structured 落盘 gate（现仅 `use_speaker_recognition`）扩展到本模式；
   - plain 结构化产物写 provenance `"mode": "plain_structured"`；
   - `llm_calibrated.txt` 继续生成（无前缀变体，保 export/raw 兼容）。
2. ~~**S6 — 集成测试**~~（已完成 `6ca788a`）：开关开全链路（plain 源 → llm_processed.json 落盘 →
   渲染带 `dlg-{i}` 锚点 → 章节 fingerprint 匹配 → `jump_ok=1`）；
   开关关回归（plain 路径行为与现状一致）。验收全集见 TASKS.md T8「完成标准」。
3. ~~**review 循环**~~（已关闭，gate 达成：R1/R2 连续两轮无新增 P1）：独立 subagent review ≤20 轮；P1 必修，P2/P3 可判「接受不修」
   记 `REVIEW-LOG.md` backlog；gate = 连续 2 轮无新增 P1。

## 纪律提醒

- 开关 `llm.structured_calibration_for_plain` 默认 **false**（暗启动，T9 验收后才翻 true）；
- `llm_processed.json` 序列化全文不含 "unknown"（硬断言）；
- 减法优先，新机制仅用于消除 P1；
- 勿把 worktree 本地 config 的 `min_chapters_threshold=1000` 当生产默认合入（示例默认 10000）。
