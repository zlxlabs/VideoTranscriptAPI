# CacheManager God Object 拆分 —— 交接 Session

> Session ID：`260723-0214-cachemanager-decompose`
> 创建：2026-07-23 02:14 (EDT)
> **状态：已被取代 — 2026-07-23 第一性原理复核后，执行交接移至 `docs/sessions/260723-0246-cachemanager-stage1/HANDOFF.md`（范围锁定 Stage 1，Stage 2+ 降级为机会主义重构、不排期）。本文档保留作 graphify 原始分析与 Stage 2+ 拆分草图的备忘；下方 Prompt 已过时，不要使用。**
> 类型：**重构规划 + 最小风险起步批**
> 上游来源：graphify 知识图谱分析（2026-07-23）— 详见 `graphify-out/GRAPH_REPORT.md`
> 基线分支：`main`
> **建议工作分支**：`refactor/cachemanager-decompose-stage1`（在 worktree 内）
> 部署机制：本仓库 `.github/workflows/` 仅 `gate.yml`，**未接入 D3**；docker 部署走 `docker/push_to_ghcr.sh` + `pull_and_deploy.sh`，本 session 与部署无关。

---

## 新 Session 直接粘贴的 Prompt

复制下面整段作为新 session 的**第一条消息**：

```text
你要在 VideoTranscriptAPI 仓库中规划并启动「CacheManager God Object 拆分」重构。全过程使用中文和我沟通，console 输出优先使用英文。先阅读仓库根目录 CLAUDE.md 与 AGENTS.md，并严格遵守（uv 虚拟环境、测试放 tests/、日志走 setup_logger、console 无中文 emoji、高内聚低耦合）。

═════════════════════════════════════════════════════════
【硬性要求：所有代码改动必须在新 worktree 中进行】
═════════════════════════════════════════════════════════

禁止在主工作区 `/home/zlx/projects/personal/VideoTranscriptAPI`（main）上直接改业务代码。

开工后**第一步**必须新建独立 worktree + 分支：

    BASE=/home/zlx/projects/personal/VideoTranscriptAPI
    WT=/home/zlx/projects/personal/VideoTranscriptAPI-worktrees/cachemanager-decompose
    git -C "$BASE" fetch origin
    git -C "$BASE" worktree add -b refactor/cachemanager-decompose-stage1 "$WT" main
    cd "$WT"
    uv sync --extra dev

之后：
- 所有文件编辑、pytest、commit 只在该 worktree 内进行。
- 全程不 push 镜像、不部署、不合并 main，除非用户明确授权。
- 主仓 main 仅用于只读对照或文档镜像。

═════════════════════════════════════════════════════════
【背景：为什么做这件事】
═════════════════════════════════════════════════════════

2026-07-23 跑了一次 graphify，对全仓 477 个文件建图（8767 节点 / 16749 边 / 384 社区）。报告显示 CacheManager 是图谱里**断层第一**的 God Object：

| 指标 | CacheManager | 第 2 名 (LLMConfig) | 说明 |
|---|---|---|---|
| 度数 | **265** | 152 | 是第 2 名的 1.74× |
| betweenness | 0.145 | 0.084 | 横跨 60+ 社区 |
| INFERRED 边 | 145 | 103 | 模型推断关系多，需验证 |

文件规模：
- `src/video_transcript_api/cache/cache_manager.py` —— **3420 行单类**
- **52 个方法**（35 个公开）

它至少背了 **11 个不相关的职责**（按方法名归类）：

  1. SQLite 连接池 + busy timeout          → _get_connection / _apply_connection_busy_timeout_ms
  2. Schema 迁移                            → _init_database / _migrate_database / _rebuild_task_status_table
  3. 媒体级并发锁                           → media_lock / terminal_archive_lock
  4. 缓存文件路径 + 读写                    → save_cache / get_cache / list_cache / _get_file_path
  5. 说话人映射存取                         → get/save/invalidate_speaker_mapping
  6. LLM 结果 + 状态存储                    → save_llm_result / save_llm_status / invalidate_llm_status
  7. 过期清理 / 孤儿文件                    → cleanup_old_cache / cleanup_task_status / _cleanup_orphaned_artifact_files
  8. 任务 CRUD                              → create_task / update_task_status / get_task_by_* / task_exists
  9. 崩溃恢复 + 关机 drain                  → recover_orphaned_tasks / drain_non_terminal_tasks_on_shutdown / reconcile_runtime_orphaned_tasks
 10. View Token 路由 + 视图层数据组装       → get_view_data_by_token / get_cache_by_view_token / _resolve_summary_state  ← 最离谱，纯业务规则塞进了 Cache
 11. 业务去重（按 URL/媒体查重）            → get_existing_task_by_url / get_existing_task_by_media  ← 也不属于 Cache

**最关键判断**：第 10/11 条**根本不是 Cache 该干的事**，是业务逻辑泄漏到存储层。这两条外移**零风险**（不涉及并发、不涉及 schema、不涉及锁），是最理想的起步。

为什么现在还没爆：测试覆盖好（社区 6 有 102 节点的 cache 单测）、关键路径有锁、schema 迁移有版本号。God Object 不会立刻爆，它**慢慢榨干迭代速度**——每次加功能都要读 3420 行找上下文。

═════════════════════════════════════════════════════════
【目标拆分草图（最终形态，不是一次到位）】
═════════════════════════════════════════════════════════

    CacheManager (薄壳保留，向后兼容)
    ├── 委托 → ConnectionManager         (职责 1, 2)
    ├── 委托 → MediaLockPool             (职责 3)
    ├── 委托 → ArtifactStore             (职责 4, 5, 6)  ← 这个才叫 "Cache"
    ├── 委托 → TaskRepository            (职责 8)
    ├── 委托 → TaskRecoveryService       (职责 9)
    ├── 委托 → Janitor                   (职责 7)         ← 独立后台任务
    └── 删除 → ViewTokenResolver         (职责 10)        ← 挪到 api/ 或 view 层
    └── 删除 → TaskDedup                 (职责 11)        ← 挪到 application 层

策略：**Facade 保留旧 API**，新代码用拆出来的小类。零 big-bang 重写。

═════════════════════════════════════════════════════════
【本 session 范围：只做 Stage 1 —— 第 10/11 条外移】
═════════════════════════════════════════════════════════

不做整个拆分。**只把业务逻辑从 CacheManager 挪出去**，建立可复用的拆分套路（commit 模板 / 测试策略 / Facade 委托模式），为后续 stage 铺路。

Stage 1 任务清单（按顺序，每步独立 commit）：

  T1. 调研：用 graphify 列出职责 10/11 涉及方法的所有调用方
      - graphify-out/graph.json 已存在，直接跑：
        graphify query "CacheManager view token resolution and task dedup callers"
      - 产出：调用方清单 + 每个调用方期望的契约（参数、返回值、错误）
      - 写入：本 session 目录的 NOTES.md

  T2. 设计：决定 ViewTokenResolver 和 TaskDedup 的物理位置
      - 候选：src/video_transcript_api/api/view_token_resolver.py
              src/video_transcript_api/api/task_dedup.py
      - 还是建一个 application/ 层？给出推荐 + 理由
      - 写入：本 session 目录的 DESIGN.md（简短，1-2 页）

  T3. 抽离 ViewTokenResolver（TDD）
      - 先写测试：把 CacheManager 现有 view_token 相关测试复制到新类的测试文件
      - 再抽代码：新建 view_token_resolver.py，搬移 get_view_data_by_token /
        get_cache_by_view_token / _resolve_summary_state / _get_llm_config_by_view_token
      - CacheManager 保留同名方法作为薄委托（@deprecated 注释），向后兼容
      - 跑测试：tests/unit + tests/cache 全绿

  T4. 抽离 TaskDedup（TDD）
      - 同样套路：先测试，再抽 get_existing_task_by_url / get_existing_task_by_media
      - 注意：这两个方法依赖 task 表查询，可能需要一个 TaskRepository 接口或直接复用 CacheManager 的连接
      - 给出权衡：是完全独立（自带连接）还是依赖注入（接收 repository）

  T5. 更新调用方：把 api/ 和 llm/ 层的调用从 CacheManager 改为新类
      - 这步是机械替换，可以批量做
      - 跑全套测试

  T6. Codex gate：连续 2 轮 read-only review 无实质新意见
      - 命令：codex exec ... -s read-only（详见 CLAUDE.md）
      - 每轮把已修复列入 already fixed

═════════════════════════════════════════════════════════
【必读文档（在 worktree 内打开）】
═════════════════════════════════════════════════════════

1. 本交接文档：`docs/sessions/260723-0214-cachemanager-decompose/HANDOFF.md`
2. graphify 完整报告：`graphify-out/GRAPH_REPORT.md`（搜 "CacheManager"）
3. 系统架构：`docs/architecture.md`（搜 CacheManager 出现的位置）
4. 项目原则：根目录 `CLAUDE.md`（高内聚低耦合那条是这次重构的法理依据）
5. 目标文件：`src/video_transcript_api/cache/cache_manager.py`（3420 行）

graphify 报告里有几个直接相关的 Suggested Questions：
- "Are the 145 inferred relationships involving CacheManager actually correct?"
- "Why does CacheManager connect [60+ communities]?"
这些可以在 T1 调研阶段作为遍历起点。

═════════════════════════════════════════════════════════
【工程纪律】
═════════════════════════════════════════════════════════

1. **TDD**：每步先写/复制失败测试，再改实现，绿了再 commit。
2. **主脑不直接写代码**：把子任务背景/约束/测试写清，派 Agent (model: sonnet) 实现；审查 diff 与测试结果再验收。单行小改动可直接改。
3. **Facade 兼容**：CacheManager 原方法**不要直接删**，改为薄委托并加 `# Deprecated: use ViewTokenResolver` 注释，给调用方迁移窗口。
4. **每个 T 步独立 commit**：commit message 中文祈使句，例如「抽离 ViewTokenResolver，CacheManager 改为薄委托」。
5. 测试：`uv sync --extra dev` 后跑 `uv run pytest tests/unit tests/cache --junit-xml=/tmp/stage1.xml`；ini 已带 -q，不要再加。
6. CRLF / 大文件用 Edit 工具，禁止整文件覆盖写。
7. 实现后跑 Codex gate，连续 2 轮无实质新意见才算完成。
8. 进度写入本 session 目录：PROGRESS.md（commit 列表 + 测试结果）、NOTES.md（决策与偏离）。

═════════════════════════════════════════════════════════
【环境坑 & 注意事项】
═════════════════════════════════════════════════════════

1. 新 worktree 必须先 `uv sync --extra dev`。
2. pytest 已带 `-q`，再加会吞汇总行。
3. CacheManager 用了 `media_lock` 上下文管理器保护并发；本 stage 不动锁相关方法，避免引入并发 bug。
4. `_resolve_summary_state` 是"诚实状态模型"的一部分（区分 summary_status: generated/skipped_short/failed/pending/disabled），搬移时**必须保留所有状态分支**，不能简化。参考 README 的"诚实状态模型"小节和 utils/task_status.py。
5. graphify-out/ 目录不要 commit（已在缓存层），但 HANDOFF/PROGRESS/NOTES 要 commit 到 docs/sessions/。
6. 数据库连接：CacheManager 自带连接池；新类要么依赖注入接收 CacheManager（保留 Facade 性质），要么独立连接。**T4 时再决定**。

═════════════════════════════════════════════════════════
【开工检查清单（按序执行，做完再写代码）】
═════════════════════════════════════════════════════════

- [ ] 新建 worktree + 分支 `refactor/cachemanager-decompose-stage1`，cwd 切到 worktree
- [ ] `git log -1` 确认基线在 main 最新
- [ ] `uv sync --extra dev` + 冒烟：`uv run pytest tests/unit/test_cache_manager.py --junit-xml=/tmp/cm-smoke.xml`
- [ ] 阅读 `graphify-out/GRAPH_REPORT.md` 的 God Nodes / Suggested Questions 段
- [ ] 阅读 `src/video_transcript_api/cache/cache_manager.py` 第 3009-3320 行（职责 10/11 的方法体）
- [ ] 执行 T1（graphify query 调用方调研）

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
| graphify 报告 | `graphify-out/GRAPH_REPORT.md` + `graphify-out/graph.json` |
| 上游来源 | 2026-07-23 graphify 分析（本次对话） |
| 本目录 | `docs/sessions/260723-0214-cachemanager-decompose/` |
| 部署机制 | 未接入 D3；本 session 与部署完全隔离 |

## 与上游 session 的关系

| Session | 职责 |
|---------|------|
| **graphify 分析（2026-07-23 上午，非 session 目录）** | 跑图、识别 CacheManager 为 #1 God Object、产出 11 职责分类 |
| **`260723-0214-cachemanager-decompose`（本 session）** | **Stage 1：业务逻辑外移（职责 10/11），建立拆分套路** |
| 未启动的后续 stage | Stage 2 起按草图依次拆 ConnectionManager / MediaLockPool / ArtifactStore / TaskRepository / TaskRecoveryService / Janitor |

## 建议的 session 内产出物

在本目录随进度追加：

- `PROGRESS.md` — 当前任务、commit 列表、测试结果
- `NOTES.md` — T1 graphify 调用方调研结论、T4 连接策略决策
- `DESIGN.md` — T2 物理位置决策（简短，1-2 页）
- 收工时更新本 HANDOFF 顶部状态为「Stage 1 完成」

## Stage 1 完成判据

- [ ] `view_token_resolver.py` 和 `task_dedup.py` 存在并被 api/ 层调用
- [ ] CacheManager 原 4+2 个方法改为薄委托（带 `# Deprecated` 注释）
- [ ] `uv run pytest tests/unit tests/cache` 全绿
- [ ] Codex gate 连续 2 轮无实质新意见
- [ ] 本目录 PROGRESS.md 记录所有 commit hash
- [ ] CacheManager 文件行数**至少减少 200 行**（职责 10/11 的实现搬走）
- [ ] graphify 重跑后 CacheManager 度数**至少下降 30**（验证拆分有效）

最后一条是关键 — 用 graphify 自己当回归测试，确保拆分真的降低了耦合，不只是把代码挪了个地方。
