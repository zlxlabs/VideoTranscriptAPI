# 章节梗概功能 —— 接线批实施交接

> 创建时间：2026-07-19 05:13 EDT  
> 更新：2026-07-19 — 计划升 v2.1.1、任务卡落地；pr3 已合入 origin/main  
> 来源仓库：`/home/zlx/projects/personal/VideoTranscriptAPI`  
> 安全批分支：`feat/chapters-foundation`  
> 状态：安全批（T2/T3/T4/T5）完成并通过 30 轮 Codex gate；**接线规格 v2.1.1**；**已 rebase origin/main（无冲突，2292 unit+llm 绿）**；接线批（T1/T6–T10）可开工

## 权威文档

| 文档 | 路径 |
|------|------|
| 设计计划 v2.1.1 | `docs/plans/2026-07-16-chapter-outline-design.md` |
| 任务卡 | `docs/sessions/260719-0513-chapters/TASKS.md` |
| 本交接 | `docs/sessions/260719-0513-chapters/HANDOFF.md` |

## 已完成 / 未完成

| 批 | 任务 | 状态 |
|----|------|------|
| 安全批 | T2 YouTube 时间戳、T3 segments 适配器、T4 FunASR 实测、T5 ChaptersProcessor | ✅ |
| 接线批 | T1 timeline 断链、T6 管线/API、T7 前端、T8 校对推广、T9 质量、T10 文档 | ❌ 未开始 |

## Eng Review 结论（不必重跑全量）

- 设计阶段 **Eng Review CLEAR**（计划末尾 GSTACK REPORT）；Codex 设计复核 14 条已折入 v2.1。
- 2026-07-19 对照 origin/main（含 pr3）只读核对：架构与 D1–D6 **仍成立**。
- **不重跑**全量 `/plan-eng-review`；接线按计划 **§5.7** 十条约束实现即可。
- 实现门：Codex gate **连续 2 轮无实质新意见**（与安全批相同）。

## 接线批开工顺序

1. ~~确认 rebase~~ ✅ 2026-07-19：`origin/main` 已是祖先；`uv run pytest tests/unit tests/llm` → 2292 passed  
2. **T1** → **T6** → **T7 ∥ T8** → T9 / T10  
3. 详见 `TASKS.md`

## 新 Session 直接使用的 Prompt

```text
你要在 VideoTranscriptAPI 仓库中继续实施"长逐字稿章节梗概"功能的接线批。全过程使用中文和我沟通，console 输出优先使用英文。先阅读仓库根目录 CLAUDE.md / AGENTS.md。

【权威文档】
- docs/plans/2026-07-16-chapter-outline-design.md（v2.1.1，尤其 §5.1–5.7、§12）
- docs/sessions/260719-0513-chapters/TASKS.md
- docs/sessions/260719-0513-chapters/HANDOFF.md

【背景】
安全批 T2–T5 已在 feat/chapters-foundation 完成并通过 30 轮 Codex gate。
pr3（#12/#13/#14）已合入 origin/main。设计 Eng Review 已 CLEAR，全量不重跑；接线遵守 §5.7。

【第一步：核实 git】
git fetch origin
确认 main 与 origin/main 对齐；feat/chapters-foundation 已 rebase 到 origin/main。
若发现与文档不符，停下报告。

【第二步：按 TASKS.md 实施】
依赖：T1 → T6 → T7/T8 并行。
主脑不直接写代码：派 Agent（model: sonnet），你审查 diff 与测试。
TDD；小步 commit；完成实现后 Codex gate 连续 2 轮干净。
全程不 push、不合并 main、不部署，除非用户明确授权。

【关键约束速记】
1. ProcessingOptions 在 api/processing_options.py；chapters 未指定则跟随 summarize，禁止默认恒 True
2. need_chapters 状态敏感（见计划 §5.1 表），不要只抄 need_summary
3. _save_llm_results：media_lock + write-ahead invalidate_llm_status
4. recalibrate：GENERATED chapters 强制重算（≠ summary 仅缺失 backfill）
5. 时间工具唯一权威：transcriber/segments.py
6. prompt 外部串必须 _flatten_for_prompt；start_seg 是原始列表下标
7. uv sync --extra dev；pytest 勿再加 -q
```

## 快速参考

| 项目 | 值 |
|---|---|
| 主仓库 | `/home/zlx/projects/personal/VideoTranscriptAPI` |
| 安全批 worktree | `/home/zlx/projects/personal/VideoTranscriptAPI-worktrees/chapters-foundation` |
| 安全批分支 | `feat/chapters-foundation` |
| 计划 | v2.1.1（§5.7 接线修订） |
| pr3 | 已合 origin/main（#12/#13/#14） |
| ProcessingOptions | `src/video_transcript_api/api/processing_options.py` |
| 剩余任务 | T1、T6、T7、T8、T9、T10 |
| Eng Review | 设计 CLEAR；全量不重跑；实现用 Codex gate |
