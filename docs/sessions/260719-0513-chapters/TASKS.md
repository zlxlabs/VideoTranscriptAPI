# 章节梗概接线批 —— 任务卡（v2.1.1）

> 创建：2026-07-19  
> 计划：`docs/plans/2026-07-16-chapter-outline-design.md`（v2.1.1）  
> 分支：`feat/chapters-foundation`（rebase 到含 pr3 的 origin/main 后开工）  
> 纪律：TDD；主脑派 subagent（sonnet）实现；连续 2 轮 Codex gate 干净才收工；不 push / 不合并 main，除非用户授权。

## 依赖与泳道

```
T1 (timeline 断链) ──► T6 (管线/状态/API) ──┬──► T7 (前端)
                                            ├──► T8 (阶段二校对，P2，可并行)
                                            └──► T10 (文档，可偏后)
T5 已完成 ──► T9 (真实样本质量，可与 T6 后并行)
```

| 任务 | 优先级 | 依赖 | 可并行 |
|------|--------|------|--------|
| T1 | P1 | rebase 完成 | 单独先做 |
| T6 | P1 | T1 | — |
| T7 | P1 | T6（至少契约：产物路径 + dlg 索引） | 与 T8 并行 |
| T8 | P2 | T1（timeline）；建议 T6 后 | 与 T7 并行 |
| T9 | P2 | T5（可本地直接调 processor）；建议有 T1 真实 segments | 独立 |
| T10 | P2 | T6 语义稳定 | 收尾 |

---

## T1 — CapsWriter/缓存 timeline 接通 + YouTube 过渡债

**目标**：三种来源新任务都能尽力落统一契约的 segments；`get_cache` / `load_segments` 可读回。

**背景**：`save_cache(..., extra_json_data=)` 已有写能力，但 `transcription.py` 全路径未传；`get_cache` 不组装 timeline；YouTube 仍 `get_subtitle()` 纯文本。

**改动面（以符号为准）**：

- `api/services/transcription.py`：CapsWriter 落盘传 `extra_json_data`；YouTube 字幕路径改 `get_subtitle_result()` 并落 segments
- `cache/cache_manager.py`：`get_cache` 读回 segments 侧车（或保证 `load_segments(cache_dir)` 能找到约定文件名）
- `transcriber/capswriter_client.py`：时间求和 `isfinite` 校验（顺带）
- YouTube：旧 `get_subtitle` 薄委托 `get_subtitle_result` 或删除平行分支重复

**约束**：

- 时间解析只 import `transcriber.segments`，禁止第二套
- 上游缺 tokens/timestamps 时不阻断任务，诚实降级
- CRLF 文件用 Edit，勿 Python 整文件覆盖

**测试**：

- 单测：extra_json 落盘 + get_cache/load_segments 读回；坏时间置 None 文本不丢
- YouTube：调用新入口后 segments 非空（fixture）
- capswriter isfinite 边界

**完成标准**：相关单测绿；无 segments 时任务仍 success 且不崩溃。

---

## T6 — 状态 / 缓存 / API / 补层全链路

**目标**：请求可开关 chapters；协调器生成；落盘状态诚实；分层补层可恢复；recalibrate 联动。

**必读**：计划 §5.1–5.5、**§5.7（R1–R10）**。

**改动面**：

| 层 | 文件 |
|----|------|
| Options | `api/processing_options.py`（**不是** transcription 内联） |
| 分层入队 | `api/services/transcription.py`（`need_chapters`） |
| 协调器 | `llm/coordinator.py`（`stage="chapters"`） |
| 队列/落盘编排 | `api/services/llm_ops.py`（`_save_llm_results`） |
| 缓存 | `cache/cache_manager.py`（`llm_type=chapters`、status 合并、DB 列） |
| 分析器 | `cache/cache_analyzer.py` |
| 审计 | `api/routes/audit.py` |
| 常量 | `utils/llm_status.py`（`ChaptersStatus` 已在 foundation） |

**硬约束（pr3 后）**：

1. `chapters=None`（未指定）→ normalize 后等于 `summarize`；禁止默认恒 True  
2. `need_chapters` 状态敏感（GENERATED+文件 / SKIPPED_* 满足；FAILED/DISABLED/缺省可重跑）  
3. 写入走 `media_lock` + write-ahead `invalidate_llm_status`  
4. recalibrate：原 GENERATED chapters **强制重算**（≠ summary 仅缺失 backfill）  
5. 仅 chapters=true **不**单独触发 title LLM  
6. 外部串进 prompt 必经 `_flatten_for_prompt`

**测试（优先单测，再小集成）**：

- normalize 矩阵：未传 / 显式 true / 显式 false × summarize 组合  
- need_chapters 各状态  
- save 合并不抹掉 calibration/summary  
- suppress 不覆盖已有 GENERATED  
- recalibrate 联动重算  
- 迁移幂等  

**完成标准**：mock LLM 下 FunASR 路径可出 `llm_chapters.json` + 正确 status；分层二次请求不重复付费（满足态）。

---

## T7 — 前端章节区块 + 锚点 + TOC XSS

**依赖**：T6 产物契约（`llm_chapters.json`、`start_seg` 原始下标）。

**改动面**：

- `utils/rendering/dialog_renderer.py`：`id="dlg-{i}"`、`render_chapters_html()`
- `api/routes/views.py`：`_prepare_success_view`
- `web/templates/transcript.html` / `base.html`
- `web/static/js/floating-toc.js`：DOM API，禁 `insertAdjacentHTML` 拼用户/章节标题

**测试**：XSS 断言（`<script>`/`onerror` 不执行）；fingerprint 不一致去链接；手测 TOC 三组。

---

## T8 — 阶段二：逐段校对推广（P2，独立开关）

**开关**：`llm.structured_calibration_for_plain`（默认 true，可回退）。

**改动面**：`speaker_aware_processor.py` 等 — has_speaker 贯穿；无 speaker 不塞 unknown；按长度合并；独立分块参数。

**完成标准**：开关关闭 = 与现状行为回归一致；开启后 plain 源可有时间块锚点。

---

## T9 — 真实样本质量验收（P2）

3–5 个长转录本地跑 `ChaptersProcessor`，人工评切分与梗概后定稿 prompt。不阻塞 T6 接线，但上线前建议完成。

---

## T10 — 功能文档（P2）

- 新增 `docs/features/chapters.md`
- 更新 `docs/features/processing_options.md`（chapters 字段、默认跟随 summarize、分层矩阵）

---

## 派发 subagent 时粘贴模板

```text
你在 VideoTranscriptAPI 的 feat/chapters-foundation 分支上实现 【T?】。
全过程中文沟通，console 英文。遵守仓库 CLAUDE.md / AGENTS.md。

必读：
1. docs/plans/2026-07-16-chapter-outline-design.md（尤其 §5.1–5.7、§12）
2. docs/sessions/260719-0513-chapters/TASKS.md 中本任务整卡
3. 若改时间：只 import transcriber.segments

约束：TDD；小步 commit；不 push；改 CRLF 文件用 Edit。
先写失败测试，再实现，跑 uv sync --extra dev 后相关 pytest（看 exit code 或 junit，勿再加 -q）。
```

## 环境坑（摘自 HANDOFF）

1. 新 worktree 先 `uv sync --extra dev`  
2. pytest 已有 `-q`，勿再加 `-q`  
3. 时间工具唯一权威：`transcriber/segments.py`  
4. prompt 安全边界：`_flatten_for_prompt`  
5. `start_seg`/`end_seg` = 原始列表下标  
