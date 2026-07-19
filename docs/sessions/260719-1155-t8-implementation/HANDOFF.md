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

1. **S4 — llm_ops 接线**（`api/services/llm_ops.py`，当前未接线，grep 无
   `plain_structured`/`has_speaker` 痕迹）：
   - `_prepare_llm_content`：**仅 `calibrate_requested=True`**（含 recalibrate）且
     plain 源有 segments → 返回 list（协调器 isinstance 路由自然生效）；
     calibrate=false 补层任务维持纯文本（防指纹永久 mismatch → 永久 nolink）；
   - structured 落盘 gate（现仅 `use_speaker_recognition`）扩展到本模式；
   - plain 结构化产物写 provenance `"mode": "plain_structured"`；
   - `llm_calibrated.txt` 继续生成（无前缀变体，保 export/raw 兼容）。
2. **S6 — 集成测试**：开关开全链路（plain 源 → llm_processed.json 落盘 →
   渲染带 `dlg-{i}` 锚点 → 章节 fingerprint 匹配 → `jump_ok=1`）；
   开关关回归（plain 路径行为与现状一致）。验收全集见 TASKS.md T8「完成标准」。
3. **review 循环**：独立 subagent review ≤20 轮；P1 必修，P2/P3 可判「接受不修」
   记 `REVIEW-LOG.md` backlog；gate = 连续 2 轮无新增 P1。

## 纪律提醒

- 开关 `llm.structured_calibration_for_plain` 默认 **false**（暗启动，T9 验收后才翻 true）；
- `llm_processed.json` 序列化全文不含 "unknown"（硬断言）；
- 减法优先，新机制仅用于消除 P1；
- 勿把 worktree 本地 config 的 `min_chapters_threshold=1000` 当生产默认合入（示例默认 10000）。
