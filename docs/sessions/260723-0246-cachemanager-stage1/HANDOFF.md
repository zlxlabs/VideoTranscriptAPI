# CacheManager 拆分 Stage 1（业务逻辑外移）—— 交接 Session

> Session ID：`260723-0246-cachemanager-stage1`
> 创建：2026-07-23 02:46 (EDT)
> **状态：未启动（裁决完成、交接就绪，等待新 session 接手执行）**
> 类型：**范围收敛后的重构执行**
> 上游来源：graphify 分析与原始规划见 `docs/sessions/260723-0214-cachemanager-decompose/HANDOFF.md`（已被本文档取代，保留作 Stage 2+ 备忘）
> 基线分支：`main`
> **建议工作分支**：`refactor/cachemanager-decompose-stage1`（在 worktree 内）
> 部署机制：本仓库 `.github/workflows/` 仅 `gate.yml`，**未接入 D3**；本 session 与部署无关。

---

## 2026-07-23 第一性原理复核裁决（已定稿，执行 session 不要重新论证）

对原规划做了一轮「问题是否真实存在、是否有必要重构」的复核，结论：

**诊断正确，剂量减半。**

事实核查（全部属实）：
- `cache_manager.py` 确为 3420 行 / 52 方法。
- 高频改动区：近 3 个月 244 个 commit 中 12 个动了此文件；已过审的章节功能（`docs/plans/2026-07-16-chapter-outline-design.md`）还要继续改它至少 4 处。
- **职责 10/11 的业务逻辑泄漏已造成过真实 bug**：`d3d8d2a`（view token 排序遗漏 calibrating 状态，查看页误显旧失败记录）与 `ee092d1`（同款排序 bug 在 dedup 两方法复现）。同一业务规则藏在缓存层两处各坏一次——这是外移的实证理由，不是图谱美学。

打折项（原规划说得比实际严重的部分）：
- 图谱指标是软证据：存储层天然高度数，265 度中 145 条是模型推断边。churn × 每次改动摩擦才是重构依据。
- 「graphify 度数下降 ≥30」是指标游戏（Goodhart），**从硬性判据降级为观察项**。
- `ee092d1` 已提取共享排序常量，最疼的重复 bug 已修；Stage 1 的边际收益是分层清晰 + 文件瘦身。

范围裁决（项目背景：个人开源项目，用户 3-4 人，单人维护）：
- **Stage 1（职责 10/11 外移）照做**：8 个调用点 / 4 个文件，不碰锁、不碰 schema，风险接近零。
- **Stage 2+（连接池 / 锁池 / ArtifactStore / TaskRepository / TaskRecoveryService / Janitor）不排期**，降级为**机会主义重构**——以后哪个功能要动哪一块，才顺手拆那一块。拆连接池和并发锁有引入并发 bug 的真实风险，而收益（团队协作、并行开发）在此项目规模下不存在。拆分草图备忘见上游文档。
- 章节功能改的是职责 6（LLM 结果/状态），与 Stage 1 正交，互不阻塞。

---

## 新 Session 直接粘贴的 Prompt

复制下面整段作为新 session 的**第一条消息**：

```text
你要在 VideoTranscriptAPI 仓库中执行「CacheManager 拆分 Stage 1：业务逻辑外移」。全程用中文和我沟通，console 输出纯英文。先读仓库根目录 CLAUDE.md 与 AGENTS.md 并严格遵守（uv 虚拟环境、测试放 tests/、日志走 setup_logger、高内聚低耦合）。

═════════════════════════════════════════════════════════
【硬性要求：所有代码改动必须在新 worktree 中进行】
═════════════════════════════════════════════════════════

禁止在主工作区 /home/zlx/projects/personal/VideoTranscriptAPI（main）上直接改业务代码。开工后第一步必须新建独立 worktree + 分支：

    BASE=/home/zlx/projects/personal/VideoTranscriptAPI
    WT=/home/zlx/projects/personal/VideoTranscriptAPI-worktrees/cachemanager-decompose
    git -C "$BASE" fetch origin
    git -C "$BASE" worktree add -b refactor/cachemanager-decompose-stage1 "$WT" main
    cd "$WT"
    uv sync --extra dev

之后所有文件编辑、pytest、commit 只在该 worktree 内进行。全程不 push 镜像、不部署、不合并 main，除非用户明确授权。

═════════════════════════════════════════════════════════
【背景与裁决（2026-07-23 已定稿，不要重新论证范围）】
═════════════════════════════════════════════════════════

CacheManager（src/video_transcript_api/cache/cache_manager.py，3420 行 / 52 方法）混入了两块不属于缓存层的业务逻辑：

- 职责 10 · View Token 路由 + 视图数据组装（4 个方法）：
    get_view_data_by_token        (约 3051 行)
    get_cache_by_view_token       (约 3149 行)
    _resolve_summary_state        (约 3009 行)
    _get_llm_config_by_view_token (约 3397 行)
- 职责 11 · 业务去重（2 个方法）：
    get_existing_task_by_url      (约 3200 行)
    get_existing_task_by_media    (约 3259 行)

外移理由是实证而非图谱指标：
1. 高频改动区——近 3 个月 244 个 commit 中 12 个动了此文件，章节功能还要继续改它。
2. 泄漏已造成真实 bug——d3d8d2a（view token 排序遗漏 calibrating 状态）与 ee092d1（同款排序 bug 在 dedup 复现），同一业务规则藏在缓存层两处各坏一次。

范围裁决：只做 Stage 1（职责 10/11 外移）。Stage 2+（连接池/锁池/ArtifactStore/TaskRepository 等）已降级为机会主义重构，本 session 禁止扩大范围。graphify 度数变化仅作观察项，不是验收门槛。

调用方清单（2026-07-23 已用 grep 调研完成，开工时复核即可）：
- api/app.py（1 处）
- api/routes/audit.py（3 处）
- api/routes/tasks.py（2 处）
- api/routes/views.py（2 处）
共 8 个调用点、4 个文件，全部在 api/ 层。

═════════════════════════════════════════════════════════
【任务清单（按顺序，每步独立 commit）】
═════════════════════════════════════════════════════════

T1. 复核调用面：grep 上述 6 个方法，确认调用点与上表一致；记录每个调用方期望的契约（参数、返回值、错误处理）→ 写入本 session 目录 NOTES.md。无需跑 graphify。

T2. 物理位置决策 → DESIGN.md（半页即可）：
    - 倾向放进 api/ 层现有结构：api/ 下已有 services/ 目录，候选
      api/services/view_token_resolver.py 与 api/services/task_dedup.py，
      或直接 api/view_token_resolver.py
    - 不要为两个类新发明 application/ 层——个人项目，避免过度设计
    - 给出推荐 + 一段理由即可

T3. 抽离 ViewTokenResolver（TDD）：
    - 先把 CacheManager 现有 view_token 相关测试复制到新类的测试文件（红→绿）
    - 搬移职责 10 的 4 个方法；CacheManager 保留同名方法作薄委托，
      加 `# Deprecated: use ViewTokenResolver` 注释，向后兼容
    - _resolve_summary_state 是「诚实状态模型」的一部分（summary_status:
      generated/skipped_short/failed/pending/disabled），必须保留所有状态分支，
      不许简化。参考 README 诚实状态模型小节与 utils/task_status.py
    - 跑 tests/unit + tests/cache 全绿后 commit

T4. 抽离 TaskDedup（TDD）：
    - 同套路抽 get_existing_task_by_url / get_existing_task_by_media
    - 连接策略：优先依赖注入（接收 CacheManager 或其连接），保持单一连接池；
      除非有硬理由，不要自建连接
    - 注意 ee092d1 已提取共享排序常量，搬移时沿用同一常量，禁止复制排序逻辑

T5. 更新 8 个调用点改用新类（机械替换），跑全套 tests/unit + tests/cache。

T6. Codex gate：codex exec ... -s read-only（详见 CLAUDE.md），连续 2 轮 read-only review 无实质新意见；每轮把已修复项列入 already fixed。

═════════════════════════════════════════════════════════
【工程纪律】
═════════════════════════════════════════════════════════

1. TDD：每步先写/复制失败测试，再改实现，绿了再 commit。
2. 主脑不直接写代码：把子任务背景/约束/测试写清，派 Agent (model: sonnet) 实现；
   审查 diff 与测试结果再验收。单行小改动可直接改。
3. Facade 兼容：CacheManager 原方法不删，改薄委托 + Deprecated 注释。
4. 每个 T 步独立 commit，commit message 中文祈使句。
5. 测试命令：uv run pytest tests/unit tests/cache --junit-xml=/tmp/stage1.xml；
   ini 已带 -q 不要再加，以 exit code 为准。
6. CRLF / 大文件用 Edit 工具，禁止整文件覆盖写。
7. 本 stage 不碰 media_lock / 连接池 / schema 迁移相关方法，避免引入并发 bug。
8. 进度写入 docs/sessions/260723-0246-cachemanager-stage1/：
   PROGRESS.md（commit 列表 + 测试结果）、NOTES.md（决策与偏离）。

═════════════════════════════════════════════════════════
【完成判据】
═════════════════════════════════════════════════════════

- [ ] view_token_resolver.py 与 task_dedup.py 存在并被 api/ 层调用
- [ ] CacheManager 原 4+2 个方法改为薄委托（带 Deprecated 注释）
- [ ] uv run pytest tests/unit tests/cache 全绿
- [ ] Codex gate 连续 2 轮无实质新意见
- [ ] PROGRESS.md 记录所有 commit hash
- [ ] cache_manager.py 至少减少 200 行
- （观察项，非门槛）graphify 重跑后的度数变化记入 NOTES.md，可跳过

开工检查清单（按序执行，做完再写代码）：
- [ ] 新建 worktree + 分支，cwd 切到 worktree，git log -1 确认基线在 main 最新
- [ ] uv sync --extra dev + 冒烟：uv run pytest tests/unit/test_cache_manager.py --junit-xml=/tmp/cm-smoke.xml
- [ ] 阅读 cache_manager.py 第 3009-3420 行（职责 10/11 的方法体）
- [ ] 执行 T1

不要只说明准备做什么——在 worktree 里实际执行并完成任务。
```

---

## 快速参考

| 项 | 值 |
|----|-----|
| 主仓 | `/home/zlx/projects/personal/VideoTranscriptAPI` |
| 基线分支 | `main` |
| **本 session 工作 worktree（须新建）** | `.../worktrees/cachemanager-decompose` + 分支 `refactor/cachemanager-decompose-stage1` |
| 目标文件 | `src/video_transcript_api/cache/cache_manager.py`（3420 行） |
| 职责 10/11 方法体 | 约 3009-3420 行 |
| 调用方 | api/app.py、api/routes/{audit,tasks,views}.py，共 8 处 |
| 上游备忘（11 职责分类 + Stage 2+ 草图） | `docs/sessions/260723-0214-cachemanager-decompose/HANDOFF.md` |
| graphify 报告（仅背景参考） | `graphify-out/GRAPH_REPORT.md` |
| 本目录 | `docs/sessions/260723-0246-cachemanager-stage1/` |
| 部署机制 | 未接入 D3；本 session 与部署完全隔离 |

## 与上游 session 的关系

| Session | 职责 |
|---------|------|
| graphify 分析（2026-07-23 上午） | 跑图、识别 CacheManager 为 #1 God Object、产出 11 职责分类 |
| `260723-0214-cachemanager-decompose` | 原始拆分规划（已被本文档取代，保留作 Stage 2+ 备忘） |
| 第一性原理复核（2026-07-23，主仓对话） | 核实事实、确认职责 10/11 外移的实证理由、**裁决范围锁定 Stage 1** |
| **`260723-0246-cachemanager-stage1`（本 session）** | **Stage 1 执行：职责 10/11 外移** |
| ~~后续 stage~~ | **已降级为机会主义重构，不排期**——以后哪个功能动到哪块，才顺手拆那块 |

## 建议的 session 内产出物

在本目录随进度追加：

- `PROGRESS.md` — 当前任务、commit 列表、测试结果
- `NOTES.md` — T1 调用方契约复核、T4 连接策略决策、（可选）graphify 度数观察
- `DESIGN.md` — T2 物理位置决策（半页）
- 收工时更新本 HANDOFF 顶部状态为「Stage 1 完成」
