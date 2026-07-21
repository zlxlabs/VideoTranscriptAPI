# 章节梗概接线批 —— 实施 Session 交接

> Session ID：`260719-0547-chapters-wiring`  
> 创建：2026-07-19 05:47  
> **状态：T1 + T6 + T7 完成；Review R1+R2 连续无新增 P1；真机 e2e 通过（2026-07-19）**  
> **延后**：校对文本上的 CW/YouTube 章节跳转 → `DEFERRED-chapter-jump-calibrated-text.md`  
> 类型：**接线批代码实施**（T1 → T6 → T7/T8…）  
> 上游 session：`docs/sessions/260719-0513-chapters/`（核实 + 计划 v2.1.1 + rebase）  
> 基线分支：`feat/chapters-foundation` @ `ddcac6c`（含 rebase 后文档交接）  
> **工作分支**：`feat/chapters-wiring` @ worktree `.../worktrees/chapters-wiring`（**未 push**）  
> 详情：`PROGRESS.md` / `NOTES.md`（含 P2 backlog）

---

## 新 Session 直接粘贴的 Prompt

复制下面整段作为新 session 的**第一条消息**：

```text
你要在 VideoTranscriptAPI 仓库中实施「长逐字稿章节梗概」功能的接线批代码改动。全过程使用中文和我沟通，console 输出优先使用英文。先阅读仓库根目录 CLAUDE.md 与 AGENTS.md，并严格遵守（虚拟环境、测试放 tests/、日志系统、避免冗余、console 无中文 emoji）。

════════════════════════════════════════════════════════
【硬性要求：所有代码改动必须在新 worktree 中进行】
════════════════════════════════════════════════════════

禁止在主工作区 `/home/zlx/projects/personal/VideoTranscriptAPI`（main）上直接改业务代码。
禁止在既有安全批 worktree `/home/zlx/projects/personal/VideoTranscriptAPI-worktrees/chapters-foundation` 上直接堆接线改动（那是 foundation 基线参考，可只读对照）。

开工后**第一步**必须新建独立 worktree + 分支，例如（在 shell 中执行）：

    BASE=/home/zlx/projects/personal/VideoTranscriptAPI
    WT=/home/zlx/projects/personal/VideoTranscriptAPI-worktrees/chapters-wiring
    git -C "$BASE" fetch origin
    # 基线：已 rebase 的 feat/chapters-foundation（不要从旧 main 开）
    git -C "$BASE" worktree add -b feat/chapters-wiring "$WT" feat/chapters-foundation
    cd "$WT"
    uv sync --extra dev

之后：
- **所有**文件编辑、pytest、commit 只在该 worktree 内进行。
- 子任务若再并行，可再开子 worktree（从 feat/chapters-wiring 或按泳道拆分），但**默认**先在 `chapters-wiring` 单 worktree 串行 T1→T6。
- 主仓 main 仅用于只读对照或文档镜像；不要在 main 上实现功能。
- 全程不 push、不合并 main、不部署，除非用户明确授权。

════════════════════════════════════════════════════════
【背景：什么已经做完】
════════════════════════════════════════════════════════

安全批（T2/T3/T4/T5）已完成并在 `feat/chapters-foundation` 上通过 30 轮 Codex gate；2026-07-19 已 rebase 到含 pr3（#12/#13/#14）的 origin/main，无冲突，`tests/unit` + `tests/llm` 2292 passed。

已有零件（消费即可，勿重造）：
- `transcriber/segments.py`（时间解析唯一权威）
- `downloaders/subtitle_types.py` + YouTube `get_subtitle_result()`
- `llm/processors/chapters_processor.py` + prompt
- `utils/llm_status.ChaptersStatus`、config 中 chapters_* 字段
- `force_json_mode` 在 llm.py

接线批尚未开始：T1、T6、T7、T8、T9、T10。

════════════════════════════════════════════════════════
【必读文档（在 worktree 内打开）】
════════════════════════════════════════════════════════

1. `docs/plans/2026-07-16-chapter-outline-design.md`（**v2.1.1**）
   - §3 总体结构、§5.1–5.7（尤其 **§5.7 十条接线约束**）、§12 任务清单
2. `docs/sessions/260719-0513-chapters/TASKS.md`（任务卡正文）
3. `docs/sessions/260719-0513-chapters/HANDOFF.md`（上游核实结论）
4. 本 session：`docs/sessions/260719-0547-chapters-wiring/HANDOFF.md`

Eng Review：设计已 CLEAR，**不要**重跑全量 `/plan-eng-review`。实现门用 Codex gate（连续 2 轮无实质新意见）。

════════════════════════════════════════════════════════
【实施顺序】
════════════════════════════════════════════════════════

T1 → T6 →（T7 ∥ T8）→ T9 / T10

本 session 默认优先做完 **T1 + T6**（功能可跑通的最小闭环）；T7/T8 若时间不够可另开 session，但契约（`llm_chapters.json`、`start_seg` 原始下标、`#dlg-{i}`）须在 T6 文档/注释中写清。

### T1 — timeline 断链
- CapsWriter `extra_json_data` 接通落盘；`get_cache` / `load_segments` 读回
- YouTube 切 `get_subtitle_result`；旧入口薄委托；清平行分支债
- `capswriter_client` 时间求和 isfinite
- 时间逻辑只 import `transcriber.segments`

### T6 — 管线/状态/API/补层（严格按 §5.7）
- Options：`api/processing_options.py`（**不是** transcription 内联）
- `chapters` 未显式指定 → 跟随 `summarize`；**禁止**默认恒 True
- `need_chapters` 状态敏感（§5.1 表），不要只抄 need_summary
- `_save_llm_results`：`media_lock` + write-ahead `invalidate_llm_status`
- recalibrate：原 GENERATED chapters **强制重算**（≠ summary 仅缺失 backfill）
- 仅 chapters=true **不**单独触发 title LLM
- prompt 外部串必经 `_flatten_for_prompt`

### T7 / T8 / T9 / T10
见 TASKS.md；T7 依赖 T6 产物契约；T8 为 P2 独立开关。

════════════════════════════════════════════════════════
【工程纪律】
════════════════════════════════════════════════════════

1. **TDD**：先写失败测试，再改实现，绿了再 commit；小步多次，不攒大 diff。
2. **主脑不直接写代码**：把子任务背景/约束/测试写清，派 Agent（`model: sonnet`）实现；你审查 diff 与测试结果再验收。
3. 每个子任务完成后在 worktree 内 commit；相关发现同步回计划或本 session 笔记（可写 `NOTES.md`）。
4. 实现后跑 Codex gate（`codex exec ... -s read-only`），**连续 2 轮无实质新意见**才算完成；每轮把已修复列入 already fixed。
5. 测试：`uv sync --extra dev` 后跑；看 exit code 或 `--junit-xml`；**不要**再加 `-q`（ini 已有 -q）。
6. CRLF 文件用 Edit 工具，禁止 Python 整文件覆盖写。
7. 过程中进度写入本 session 目录：`docs/sessions/260719-0547-chapters-wiring/`（如 PROGRESS.md / NOTES.md）。

════════════════════════════════════════════════════════
【环境坑】
════════════════════════════════════════════════════════

1. 新 worktree 必须先 `uv sync --extra dev`
2. pytest 已带 `-q`，再加会吞汇总行
3. `transcriber/segments.py` 是时间解析唯一权威
4. `Chapter.start_seg/end_seg` = 原始输入列表下标
5. json_schema 模式无重试；章节用 `force_json_mode="json_object"`

════════════════════════════════════════════════════════
【开工检查清单（按序执行，做完再写代码）】
════════════════════════════════════════════════════════

- [ ] 新建 worktree + 分支 `feat/chapters-wiring`（或等价名），cwd 切到 worktree
- [ ] `git log -1` 确认基线含 chapters_processor / segments / v2.1.1 文档
- [ ] `uv sync --extra dev` + 快速冒烟：`uv run pytest tests/unit/test_chapters_processor.py tests/unit/test_segments_adapter.py --junit-xml=/tmp/wire-smoke.xml`
- [ ] 阅读 §5.7 与 TASKS.md T1
- [ ] 开始 T1（TDD）

不要只说明准备做什么——在 worktree 里实际执行并完成任务。
```

---

## 快速参考

| 项 | 值 |
|----|-----|
| 主仓 | `/home/zlx/projects/personal/VideoTranscriptAPI` |
| 基线分支 | `feat/chapters-foundation` @ `b578608`（+70 commits vs origin/main，未 push） |
| **本 session 工作 worktree（须新建）** | 建议：`.../worktrees/chapters-wiring` + 分支 `feat/chapters-wiring` |
| 只读参考 worktree | `.../worktrees/chapters-foundation`（勿在其上堆接线 diff） |
| 计划 | `docs/plans/2026-07-16-chapter-outline-design.md` v2.1.1 |
| 任务卡 | `docs/sessions/260719-0513-chapters/TASKS.md` |
| 上游交接 | `docs/sessions/260719-0513-chapters/HANDOFF.md` |
| 本目录 | `docs/sessions/260719-0547-chapters-wiring/` |

## 与上游 session 的关系

| Session | 职责 |
|---------|------|
| `260719-0513-chapters` | 核实 git、计划 v2.1.1、TASKS、rebase foundation、基线测试 |
| **`260719-0547-chapters-wiring`（本 session）** | **新 worktree 内执行 T1/T6… 代码改动** |

## 建议的 session 内产出物

在本目录随进度追加（由实施 session 维护）：

- `PROGRESS.md` — 当前任务、commit 列表、测试结果
- `NOTES.md` — 偏离计划的决策与原因
- 收工时更新本 HANDOFF 顶部状态
