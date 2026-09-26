# PR3 遗留问题实施交接

> 创建时间：2026-07-15 06:35 EDT  
> 来源仓库：`~/projects/personal/VideoTranscriptAPI`  
> 来源分支/提交：`main@39b55701e8f910853e2870712cd199d401a50f83`  
> 状态：Eng Review 已通过，等待在独立 worktree 中实施

## 新 Session 直接使用的 Prompt

复制下面整段内容作为新 session 的第一条消息：

```text
你要在 VideoTranscriptAPI 仓库中实施 PR3 审查后确定的全部工程改进。全过程使用中文和我沟通，console 输出优先使用英文。先阅读仓库根目录 AGENTS.md，并严格遵守其中约定。

【硬性要求：所有实现必须在新 worktree 中进行】

当前主工作区：
~/projects/personal/VideoTranscriptAPI

当前主工作区包含尚未提交的 review 计划和 session 交接文档。它们属于用户已有工作，禁止丢弃、覆盖、stash、reset、checkout 或清理。主工作区只能读取，不能用于任何代码、测试、配置或实施文档修改。

开始任务后的第一组动作必须是：

1. 在主工作区只读检查 `git status --short`、`git worktree list` 和目标分支是否存在。
2. 从提交 `39b55701e8f910853e2870712cd199d401a50f83` 创建全新的工作分支和 sibling worktree。建议：
   - branch: `feat/pr3-review-hardening`
   - worktree: `~/projects/personal/VideoTranscriptAPI-worktrees/pr3-review-hardening`
3. 推荐命令：
   `git -C ~/projects/personal/VideoTranscriptAPI worktree add -b feat/pr3-review-hardening ~/projects/personal/VideoTranscriptAPI-worktrees/pr3-review-hardening 39b55701e8f910853e2870712cd199d401a50f83`
4. 如果分支名或目录已存在，不要删除、覆盖或复用未知 worktree；改用带时间后缀的新分支和新目录。
5. 创建后立即 `cd` 到新 worktree，验证 `pwd`、`git status --short`、`git rev-parse --show-toplevel` 和 `git branch --show-current`。
6. 从此以后，所有代码编辑、测试产生的文件、迁移、配置示例、实施文档和本地提交都必须发生在新 worktree。每次编辑或测试前先确认当前 cwd 位于新 worktree，禁止回到主工作区改文件。

不要只说明你准备创建 worktree，必须实际创建并在其中完成任务。

【权威输入】

主计划文件（当前位于原工作区，作为只读权威来源）：
~/projects/personal/VideoTranscriptAPI/docs/plans/2026-07-15-pr3-review-retrospective.md

重点阅读该文件的：
- “四、CEO Review + Eng Review 后的执行结论”
- “五、目标架构与数据流”
- “六、六个串行 PR”
- “七、测试与失败模式”
- “十、NOT in scope”
- “十二、Implementation Tasks”
- “GSTACK REVIEW REPORT”

完整测试矩阵：
~/.gstack/projects/zj1123581321-VideoTranscriptAPI/zlx-main-eng-review-test-plan-20260715-061341.md

机器可读实现任务：
~/.gstack/projects/zj1123581321-VideoTranscriptAPI/tasks-eng-review-20260715-062806.jsonl

Eng Review 状态：CLEAR，48 个问题或测试缺口已折入计划，0 critical gap，0 unresolved decision。相关测试基线为 226 passed。

【执行目标和顺序】

保留完整目标，但严格按下面 6 个失败域串行实施。不要把所有内容揉成一个不可审查的大改动。每阶段先实现、补测试、更新对应文档并验证，再进入下一阶段；形成 6 个清晰、可独立审查的本地提交。除非用户明确授权，不 push、不创建远程 PR、不部署线上服务。

1. PR1：配置与 RuntimeContext 生命周期
   - 使用 FastAPI lifespan 和小型 RuntimeContext。
   - 删除 route/service import 阶段对配置、数据库、队列、executor、LLM 等运行时对象的 eager 绑定。
   - bootstrap logger → 严格配置 → production logger；关闭时释放 worker、executor 和连接。
   - 配置结构和身份字段始终严格；外部后端凭据只在相应功能启用时严格。
   - 缺配置允许安全 import，但应用启动失败并给出清晰错误。
   - 增加无副作用的 `main.py --check-config`，不能连接外部服务、迁移 DB 或启动线程。

2. PR2：用户身份与 ProcessingOptions
   - 缺字段、空/重复 user_id、保留值 legacy_user、非法权限全部拒绝。
   - reload 使用 validate-then-swap；失败保留 last-known-good。
   - user_id 是永久稳定、不可复用的审计主体，但不要引入身份注册表或墓碑系统。
   - 建立唯一 ProcessingOptions 规范化模型；默认 calibrate=true、summarize=true、infer_speaker_names=true。
   - 删除 transcription/llm_ops 中重复默认值，日志必须遮罩 token。

3. PR3：不可变任务终态
   - task_status 保存规范化 processing_options、提交者归属和完成时的 merged snapshot。
   - task_status 是一次请求的不可变终态；llm_status.json 是可继续推进的媒体当前状态。
   - artifact/status 原子写入后，使用 compare-and-set 写一次终态。
   - `force=True` 不得覆盖终态，只能用于合法的非终态恢复。
   - repository 清理/保存失败必须抛错；周期维护层负责记录和重试，禁止返回 0 伪装成功。

4. PR4：审计快照和历史查询
   - audit.db 新增 audit-owned task_audit_snapshots，迁移和回填必须幂等。
   - 归档成功后才能删除 task；失败则保留并由 repair 重试。
   - 历史查询只访问 audit.db，不再每请求 ATTACH cache.db。
   - live view_token 只在任务存活时保留；删除任务前清空并标记“内容已过期”。
   - 晚期 404 审计允许 task_id=NULL。
   - 增加最小必要索引；archive/repair/cleanup 每批最多 500 条。

5. PR5：说话人 artifact 与真实零 LLM
   - infer_speaker_names 是独立开关，默认 true。
   - false 时可复用完整有效映射；cache miss 只返回通用标签且不能污染完成缓存。
   - artifact 使用 video_cache.files_loc，包含 schema version、转录/diarization 输入指纹、speaker 集合和来源标记。
   - 临时文件 + 原子替换，并复用现有 media lock；不要新建第二套锁系统。
   - calibrate、summarize、infer_speaker_names 全部关闭时，LLMClient.call 必须为 0。
   - 如果 infer_speaker_names=true，即使 calibrate=false、summarize=false，也允许姓名推断调用 LLM。这是已拍板的产品语义，不要重新讨论。
   - 不改变现有 prompt 文本，不做无关质量调参。

6. PR6：部署硬化代码
   - 镜像使用唯一 tag，并在部署身份中固定 digest，禁止依赖漂移的 latest。
   - restart 前使用候选镜像运行无副作用 `--check-config`；失败时当前服务保持运行。
   - 记录旧 digest，候选健康检查失败时恢复旧 digest 并复查健康。
   - 部署目标信息来自 `docker/deploy_targets.json`：`n305:/opt/media/VideoTranscriptAPI`。
   - 本 session 只实现和测试部署能力；没有用户再次明确授权时，不执行真实 n305 部署。

【产品边界，禁止擅自改变】

- 系统的设计目的就是处理完成后方便公开分享，不是严格多租户 SaaS。
- view_token 是不可猜测的公开只读 capability；底层媒体缓存和结果允许跨提交者复用。
- task_id 可以作为进度查询 capability。
- user_id 只用于提交归属、私有审计历史和用量统计，不限制公开内容读取。
- 重新校对、删除等写操作仍要求认证和相应权限；公开访客只读。
- 不收紧 `/api/audit/stats` 的 is_multi_user_mode/total_users；这是已接受风险。

【明确不做】

- 不做严格租户隔离、每用户复制媒体缓存或私有化分享链接。
- 不做用户身份注册表/墓碑、自动历史身份迁移。
- 不做新审计 UI 或页面重设计。
- 不让审计记录延长正文/转录访问期。
- 不全面重写巨型 CacheManager，不引入 repository/unit-of-work 框架。
- 不改 prompt，不搭车实现 TODOS.md 里的速率限制、TTL/LRU、专名替换。
- 不建设多机蓝绿或新的部署平台。

【工程方法】

- 先读现有代码和测试，复用 CacheManager、AuditLogger、media lock、FastAPI dependency、pytest 和现有部署脚本。
- 使用 `uv`，优先 `uv sync`、`uv run pytest ...`。
- 所有文件修改使用 apply_patch；保留用户已有改动，不执行 destructive git 命令。
- 对明确、可逆、影响小的工程选择，按第一性原理和最佳实践直接决定，不要提问。
- 只有影响产品边界、数据兼容、安全策略或不可逆操作且确实拿不准时才向用户提问；提问前用大白话讲完整背景、选项差异和推荐。
- 每完成一个阶段先运行该阶段的定向测试，再运行相关 unit/integration 回归；最终运行完整相关回归和 `git diff --check`。
- 任何 migration 都必须覆盖旧版本升级、重复执行、失败回滚/启动阻断。
- 不要用跨 SQLite/JSON 的“伪原子事务”；用明确顺序、幂等写入和 repair 保证恢复。

【现有回归基线】

先在新 worktree 建好环境，然后运行以下基线。如果失败，先确认是否为 worktree 环境问题，不要修改原工作区：

uv run pytest -q \
  tests/unit/test_history_routes.py \
  tests/unit/test_llm_ops_title_generation.py \
  tests/unit/test_speaker_inferencer.py \
  tests/unit/test_processing_options.py \
  tests/unit/test_user_manager_permissions.py \
  tests/unit/test_audit_logger.py \
  tests/cache/test_periodic_maintenance.py \
  tests/cache/test_task_status_cleanup.py \
  tests/unit/test_llm_ops_layered_cache_race.py \
  tests/integration/test_task_status_lifecycle.py \
  tests/integration/test_layered_cache.py

历史结果：226 passed。当前已知 warning 包括 FastAPI on_event 弃用和部分 Pydantic 弃用；lifespan warning 应随 PR1 消失，不要把无关的全仓 Pydantic 重写塞进本任务。

【完成标准】

- 6 个阶段全部实现，每阶段有独立、清晰的本地提交和验证证据。
- 新增成功、错误、并发、迁移、恢复/回滚测试，与测试矩阵一致。
- 文档同步修正 task_status 与 llm_status 语义、配置启动规则、ProcessingOptions、公开分享边界和部署流程。
- 最终报告列出：worktree/branch、每个阶段提交、修改文件、测试命令和结果、剩余风险。
- 不要修改、清理或提交原主工作区中的未提交文档。

现在开始：先实际创建并进入新 worktree，然后报告 worktree 路径、分支和基线状态，再持续实施，不要停留在计划复述。
```

## 当前工作区状态

- `main@39b55701e8f910853e2870712cd199d401a50f83`
- 原工作区只有 review 计划处于未提交修改状态；创建本交接文档后，本目录也会成为未跟踪文件。
- 没有代码修改、没有 staged 文件、没有第二个 worktree。
- review 阶段相关定向测试基线：226 passed。

## 已确定的关键决策

- 目标不删减，但以 6 个串行失败域实施。
- RuntimeContext/lifespan 先建立依赖生命周期。
- 任务不可变终态必须先于 audit snapshot。
- audit archive 使用幂等 upsert + archive-before-delete + repair，不构造跨库事务。
- 公开分享是产品能力，不能被误改为严格租户 ownership。
- 说话人推断独立于校对/总结；真正零 LLM 由三个 feature gate 共同决定。
- 部署固定 digest，preflight 失败不重启，健康失败恢复旧 digest。

## 新 Session 需要特别留意

- 新 worktree 从已提交的 `39b5570` 创建，所以看不到原工作区尚未提交的 review 计划增补；必须通过上面的绝对路径只读参考权威计划。
- 不要为了把计划带入 worktree 而在原工作区 commit、stash、reset 或复制覆盖文件。
- 真实部署属于外部状态变更，本交接只授权实现和测试部署代码，不授权上线。
