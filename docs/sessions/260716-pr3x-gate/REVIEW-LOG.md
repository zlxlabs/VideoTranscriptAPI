# PR3 加固分支 Gate Review 记录

> 时间：2026-07-16
> 分支：`feat/pr3-review-hardening`（worktree：`VideoTranscriptAPI-worktrees/pr3-review-hardening`）
> 前置：6 个 PR 域实现 + 22 个前期审查修复已在分支上（基线 `39b5570`，设计文档见
> `docs/plans/2026-07-15-pr3-review-retrospective.md` 与主工作区 `docs/sessions/260715-0635-pr3x/HANDOFF.md`）
> 通过标准：独立 gate review 连续 2 轮无新增实质性意见
> 结果：**第 6、7 轮连续 PASS，通过 gate**

## 评审轮次与结论

| 轮次 | 视角 | 结论 | 实质性发现 |
|---|---|---|---|
| 1A | PR1–PR4 设计不变式逐条核对 | PASS | 3 条 + 1 条合并风险 |
| 1B | PR5–PR6 + 测试质量 + 文档同步 | PASS | 2 条 |
| 2 | 修复验证 + 横切扫掠（时区/墓碑/异常路径/旧数据兼容） | PASS | 0 |
| 3 | 部署 shell 逐行实弹 + API 契约 + 旧库实弹迁移 + 配置样例 | FAIL | 1 条 |
| 4 | 通知层 + LLM 错误路径 + UserManager 并发 + git 历史卫生 | FAIL | 1 条 |
| 5 | 净 diff 生产代码终读 + 真启动冒烟 + speaker falsy 全仓排查 | FAIL | 1 条 |
| 6 | validate↔from_dict 逐键对齐 + 测试工程质量 + secrets 金丝雀 | **PASS** | 0（计数 1/2） |
| 7 | 收官综合判定 + 样例配置端到端实弹 + 文档终检 | **PASS** | 0（计数 2/2） |

## 实质性发现与修复（8 条，全部 TDD 红绿验证）

| Commit | 发现（轮次） | 修复 |
|---|---|---|
| `d161951` | files_loc 跨月回归测试锁不死跨月回归（1B） | 测试把 files_loc 改为非当前月目录后断言命中持久目录 |
| `3eb7157` | "tag 启动旧容器解析回滚 digest"仅有源码 grep 断言（1B） | fake docker 补 inspect 分支，改为行为测试 |
| `25ded69` | recalibrate 直插 task_status 缺 submitted_by/processing_options（1A） | INSERT 补两列，值与 llm_task 同源防漂移 |
| `7d7ad25` | update_task_status docstring 宣称 force 可绕过终态保护，与 CAS 实现冲突（1A） | docstring 如实描述，force 标注 deprecated |
| `6aadb57` | "启动失败必须中止 lifespan"无端到端测试（1A） | 注入抛错 context，断言 TestClient 启动失败且资源清理 |
| `6071f60` | 回滚兜底 compose stop 因 `${VIDEO_TRANSCRIPT_IMAGE:?}` 插值失败被 `\|\| true` 吞掉，故障候选容器不被停止；compose_container_ref 的 ps -q 同根因永远走硬编码回退（3） | 两处注入变量；fake docker 真实模拟插值失败，消除测试假阳性 |
| `5917aa9` | `_coerce_dialogs` 未同步 numeric-zero 修复，spk=0 折叠成 "unknown" 致预检/保存双端指纹不一致，快速返回路径对此类媒体永久失效（4） | speaker 提取对齐 `is not None` 别名链 + 跨点一致性测试 |
| `d0e046c` | validate_config 不校验 llm 段而 LLMCoordinator 启动期急切构造：缺 llm 段配置通过 `--check-config` 部署预检但启动裸 KeyError 崩溃，击穿 PR6 守门不变式（5） | validate_config 补 llm 四个硬键校验（与 LLMConfig.from_dict 逐键对齐）；新增真启动路径集成测试消除 FakeRuntimeContext 盲区 |

顺带修复（nitpick 级）：`15f633f` / `306d6b6`（注释与测试 fixture）、`59773bb`（validate_token TOCTOU KeyError→500）、`2b4b8bc`（最终异常兜底先写 FAILED 终态再通知）。

另：`1fcd3e4` 将 main 的三个新提交（`_TASK_STATUS_PRIORITY_ORDER_BY` 共享排序常量 / calibrating 排序修复 / recorder:// 跳过元数据探测）merge 进分支，人工核对常量三处使用与 content_expired 拦截共存，全量回归验证。

## 最终验证状态

- 全量套件（`tests/` 除 manual）：**1842 passed**，多轮重复全绿；真启动测试 3 连跑稳定（~2.5s）。
- `--check-config` 对 `config/config.example.jsonc` 实弹 OK；缺 llm 段/空键均以可读 ConfigError 拒绝（rc=1）。
- secrets 金丝雀探测：api_key/auth_token 不出现在任何启动失败输出与日志中。
- `git status` 干净，diff 终览无不应入库文件。

## 已接受的遗留（不阻塞，后续可另开任务）

- **main 存量债务（非本分支引入）**：14 个测试文件报 `PytestReturnNotNoneWarning`，其中 `tests/unit/test_view_token_reuse.py` 全文件无 assert 纯 return（假阳性形态）；测试套件混有真实外网用例待 mock（已有备忘）。
- **本分支范围内已声明接受**：`cache_manager.py` 等 CRLF/LF 混合行尾（建议 .gitattributes 后一次性规范化）；`update_task_status` 的 force 死参数（已标 deprecated 未删）；`--check-config` 失败以 traceback 形式退出（末行信息可读）；validate_config 不校验 log 段（缺失走默认值不炸）；退化输入下预检/保存两侧 speakers 推导路径理论分叉（FunASR 契约下不触发，建议后续统一为共享 helper）；路由 detail 回显异常文本（仅认证用户可见，基线模式）。
- **文档**："llm 配置段为启动必需"未在 README 显式成句（样例配置键齐全 + 预检错误信息可读，判定可接受）。

## 未授权事项（保持）

- 未 push、未创建远程 PR、未执行真实 n305 部署（需用户明确授权）。

## Codex gate 补充轮（2026-07-16）

> 背景：本文档记录的 7 轮 gate review 通过后，又并行产出了 `feat/pr3-hardening-part1`
> （继续在同一条历史线上叠加修复的 stacked PR）。CI Codex + 本地 Codex 各自对"最终代码"复核，
> 分别发现了对方也没覆盖到的问题。整合在独立分支 `pr3x/final`（从 `pr3x/linear` 分叉）完成：
> 3 条从 part1 移植过来，3 条全新 TDD 修复，共 7 项（其中 1 项 linear 已自行修复，仅记录不
> 重复移植）。

| Commit | 发现 | 修复 | 来源 |
|---|---|---|---|
| `619e890` | `--check-config` 不校验真实启动会加载的 `config/users.json`：空文件/损坏 JSON/重复 user_id/非法权限均能穿过预检，真启动才由 `UserManager` 构造炸 | 新增 `_validate_users_json()`，挂进 `load_and_validate_config`，直接构造 `UserManager(fallback_config=config)` 复用其校验逻辑 | 移植自 part1 `37462fa` |
| `366024a` | `recover_orphaned_tasks()` 用裸 `UPDATE` 把中断任务标 failed，绕开 `update_task_status` 的终态快照组装，产生的 failed 行 `terminal_snapshot` 恒为 NULL，违反"终态必须带快照"不变式 | 改为逐 task_id 调用 `update_task_status(..., terminal_snapshot={"recovered": True, ...})`，复用既有快照与 CAS 路径 | 移植自 part1 `8ea3ced` |
| `230d6e5` | `_LazyResource(Mapping)` 的 mixin 方法（get/keys/items/...）遮蔽被代理对象的同名方法：`llm_task_queue.get(timeout=...)` 解析到 `Mapping.get`，抛 TypeError 被消费循环的 `except Exception` 吞掉后无限重试——LLM 队列生产上永不消费，线程不死，所有启动测试全绿 | 去掉 Mapping 基类，保留手写的 `__getattr__`/`__getitem__`/`__iter__`/`__len__` 代理 | 移植自 part1 `924ae22`（该 commit 里的 `concurrent` 变量遮蔽修复 linear 已自行修复，仅此处记录、不重复移植） |
| `271e7b1` | Dockerfile `HEALTHCHECK` 探测 `/api/health`，但 health 路由无前缀挂载，真实路径是 `/livez`/`/health`——探测恒 404，候选容器永远 unhealthy，D3 部署必然回滚 | 改探测 `/livez`；同步修正 `pull_and_deploy.sh` 的无 HEALTHCHECK 回退探测路径与 README 描述，三处保持一致 | 全新发现（本地 Codex） |
| `f30052e` | `validate_config` 只校验 llm 四个硬键，`LLMConfig.from_dict` 的 `total_timeout` float() 转换、以及 4 个嵌套段（segmentation/structured_calibration/speaker_inference/quality_validation）当 dict 用这两类用法零校验，与硬键缺失同类的 boot-fatal 缺口 | 补齐这两类"存在即校验类型，不存在遵从默认值"的检查 | 全新发现（CI Codex 升级为 major） |
| `2768a15` | 观察者轮询他人任务的 `GET /api/task/{task_id}` 会写审计行，`/api/audit/history`/`/api/audit/summary` 把"存在一条属于我的审计行"等同于任务归属，轮询一次即可让他人任务连同真实快照进入自己历史并通过详情归属检查——设计允许的进度查询 capability 被放大成历史/内容访问 capability | 归属改锚定 `task_audit_snapshots.submitted_by`（提交时写入，只有 `/api/transcribe`/`/api/recalibrate` 设置），该列为 NULL 的存量旧行按提交类端点白名单兜底 | 全新发现（本地 Codex 追加） |

结构决策：stacked PR（`feat/pr3-hardening-part1` 在同一条历史线上继续叠加修复）会让后续
review 按"这一层相对上一层改了什么"切分注意力，容易对已经合入更早层、但本层 diff 里看不出
改动的旧代码重复视而不见——这次两条并行分支各自独立发现了对方遗漏的问题就是实例。改为按
**最终内容**切分（本次以 `pr3x/final` 为唯一交付树，逐条核对每个域的最终状态，而不是逐层
核对每次增量 diff）后，同一类"预检绿灯、真启动才崩溃"的模式（`users.json` 校验缺失 /
llm 嵌套段类型缺失 / `_LazyResource` 遮蔽）才被一次性扫出三个独立实例，而不是分散在两条
分支上各中一次、互相都没发现。后续多阶段加固建议优先用单分支 + 多轮 review 收敛到底，
而非 stacked PR 按历史切分。

## 已接受的设计决策（Codex review 语境）

> 背景：本地 Codex review（复刻 CI gate）对 `pr3x/final` 终态代码又报了 8 个 major，
> 其中 6 个是实质缺陷（已 TDD 修复，含 1 个需要先核实真伪、核实为真的跨进程复活
> view_token 问题），另外 2 个经甄别是已接受的设计取舍而非缺陷，记录理由如下，避免
> 后续 review 反复对同一处提出相同意见。

- **关闭超时不约束进程退出**：`RuntimeContext.aclose()` 里 `_stop_workers()` 对
  transcription/LLM worker 只做 5s 有界等待，超时会把 `resources_safe` 置为 `False`
  并保留资源不关闭（见 `_finish_close` 的 else 分支），遗留的非 daemon worker 线程
  可能继续运行到自然结束，这个有界等待本身并不能保证进程一定会退出。这在裸机长期
  运行的场景下确实是个开放问题，但本项目唯一支持的生产部署形态是容器化（D3 自动
  部署流水线，见 `deploy_targets.json` / `docs/plans/2026-03-13-docker-deployment-*.md`），
  进程级的退出边界由 `docker stop` 的 SIGTERM → 超时强制 `SIGKILL` 提供；
  `aclose()` 的 5s 有界等待要保证的只是 FastAPI lifespan 本身不会无限悬挂（这一点已由
  `test_runtime_close_is_bounded_when_llm_consumer_does_not_stop` 等测试锁定），
  残余的非 daemon worker 线程由容器终止兜底即可，不需要应用层自己再实现一层进程级
  退出保证。裸机长期运行不在本项目的支持范围内。
- **单进程内全局 loguru/notifier**：`utils/logging`、`utils/notifications` 的日志器与
  通知客户端是模块级全局单例，多个 `RuntimeContext` 并存时会共享同一份。这在生产
  环境不是问题——生产恒为单 app 单进程（同上 D3 部署模型），不存在"多个 lifespan
  同时活跃、需要互相隔离日志/通知"的场景；测试套件里出现的双 lifespan 并存（如
  `test_two_app_lifespans_own_and_close_separate_contexts`）目的是验证每个
  `RuntimeContext` 各自独立拥有并释放自己的资源（任务队列、线程池、cache_manager
  等），锁的是 context 资源所有权边界，而不是要求日志/通知也做到按进程隔离。

> 追加（本地 Codex review 第 3 轮，10 个 major）：3 条合并为预检演练范围扩展
> （llm 剩余参数/log/notification 三类启动解析器，非缺陷，是覆盖面补全）；
> 4 条逐项核实后确认为真实缺陷并已 TDD 修复（关闭清算与维护线程竞态——
> 起初凭直觉误判为伪，写可复现实验后证实是真实竞态；永久保留配置停摆周期性
> repair；recalibrate 绕过 runtime 的 UserManager；损坏 speaker artifact 导致
> 任务失败）；1 条核实后确认为真并已按最小改动修复（speaker-only 补层结果
> 未进入展示产物）；以下两条经甄别是设计取舍而非缺陷。

- **repair 跨进程复活窗口**：`repair_task_snapshots` 归档前重新 `get_task_by_id`
  查询（`9687139` 已加）把过时快照窗口收紧到"这一条任务归档前的一次重新查询"，
  但严格来说仍存在极小的 TOCTOU 缝隙——两次查询之间任务状态再次变化。本项目唯一
  支持的部署形态是单容器单 app 进程（同上 D3 部署模型），repair 与
  `cleanup_old_logs`/`cleanup_task_status` 都在同一个进程内、经
  `AuditLogger.terminal_archive_lock`（`_terminal_archive_lock`）串行化执行，不存在
  真正的多进程并发写手；跨进程场景（多个应用实例共享同一份 cache.db/audit.db
  SQLite 文件）不在本项目支持的部署形态内。残余的毫秒级窗口在"单写者"部署下不可达，
  不再进一步加固（如引入跨库事务或分布式锁）。
- **speaker artifact 指纹不含 metadata_override/模型切换**：`speaker_mapping.json`
  的 `input_fingerprint`（`SpeakerInferencer.input_fingerprint`，见
  `llm/core/speaker_inferencer.py`）只由 `speakers` 集合与转录/diarization
  `dialogs`（说话人标签+文本+时间戳）参与哈希，加上 `get_speaker_mapping` 单独
  校验的 `schema_version`——三者合起来是这份产物是否可复用的完整判据。
  `metadata_override`（标题/作者/描述）与当前配置的推断模型均不参与指纹计算，是
  刻意取舍而非遗漏：姓名推断以媒体内容（说话人是谁、说了什么）为主要依据，标题/
  作者改写或模型版本升级不应使全部历史映射作废、逼迫重烧 token 全量重新推断。
  如后续需要支持"强制重新推断"，应该走显式失效入口（如一个专门的 force_reinfer
  参数/管理端点），而不是把这些字段并入指纹自动失效——那会让每次模型升级都造成
  一次隐性的全量重新推断开销。

## 本地 Codex review 第 4 轮（6 个 major，全部为真，已 TDD 修复）

| 发现 | 修复 |
|---|---|
| `_rehearse_set_default_config_types` 只演练了 `total_timeout` 转换，SyncLLMClient 构造期还会立即消费 `refusal_keywords_url`（truthy 非 str/不可迭代时 `list()` 抛 TypeError）与 `collector_url`（truthy 非 str 时 `CollectorClient.__init__` 的 `url.rstrip("/")` 抛 AttributeError），预检看不到 | 补齐这两处的结构校验（string/list[str]/null），不构造 SyncLLMClient 本身 |
| `_parse_log_settings` 只检查 `log.max_size`/`backup_count` 是否为 int\|str 类型，未校验字符串内容是否是 loguru rotation/retention 真正能解析的形状；`setup_logger` 对无目录前缀的相对 `log.file`（如 `"app.log"`）执行 `os.path.dirname` 得到空串，`ensure_dir("")` 崩溃——独立于预检的真实启动 bug | 新增基于 `loguru._string_parsers`（私有 API，包 ImportError 降级）的 rotation/retention 字符串校验；`setup_logger` 加 `if log_dir:` 判断跳过空串 mkdir |
| `repair_task_snapshots` 用持久 OFFSET 分页，`cleanup_task_status` 按同一顺序从旧到新删除终态任务时会让剩余集合整体左移，resume 时用旧 OFFSET 续扫会跳过一整批从未扫描过的任务，集合持续增长时游标可能永远追不上 | `CacheManager.list_terminal_tasks` 分页方式改为 keyset（seek）游标（`after: (completed_at, task_id)` + 行值比较），删除排在游标之前的行不影响游标之后的可达性 |
| `GET /api/audit/summary` 的归属校验只对 `get_task_by_view_token` 优先选出的那一条任务做判定，而 view_token 允许跨任务共享（同 URL 重复提交）——共享 token 的其它合法提交者会稳定 403，且 recalibrate 改变优先级选择后 403 对象还会翻转 | `_check_ownership` 改两级判定：优先级选中任务的快路径 + 复用 `_task_attribution_condition` 在该 view_token 关联的其它任务快照范围内再找一次；内容仍是优先选中的那条，内容选择与授权解耦 |
| `_refresh_speaker_names_in_existing_structured_artifact` 用"旧 speaker_mapping 反查显示名"定位原始标签，两个原始标签共享同一旧显示名时 dict 覆盖语义丢失其中一个，导致该原始标签下全部 dialog 被误按另一个标签的新名覆盖（张冠李戴，非理论风险，测试已实测复现） | `SpeakerAwareProcessor._normalize_dialog` 新增 schema 字段 `speaker_id`（原始标签，随显示名一起保留），刷新直接按 `speaker_id` 查表，不再反查；连带修正 `_normalize_and_merge_dialogs` 的合并判据由显示名改为 `speaker_id`，避免两个同显示名的不同说话人被错误合并成一条 |
| 说话人姓名补层刷新失败只 `logger.warning`，任务仍以 success 收尾——而 `SpeakerInferencer.infer()` 早在刷新运行之前就已把新 mapping 写盘，导致"mapping 已存、展示未刷新"静默不一致，且下一次相同请求会因 `input_fingerprint` 命中新缓存而永远跳过重试 | 新增 `CacheManager.invalidate_speaker_mapping`；刷新失败时先回滚（删除 speaker_mapping.json，下一次请求视为缓存未命中）再 `raise OSError`，与相邻 "structured" 全量保存分支失败时的处理方式一致，任务状态如实反映为失败 |

**已记录的设计决策（T5 老格式兼容，非缺陷，避免后续 review 反复提出）**：若既有
`structured_data` 的 dialogs 是本次 schema 演进之前产出的（不带 `speaker_id`），
刷新函数检测到后**跳过刷新、但保留已经由 `SpeakerInferencer.infer()` 写盘的新
mapping**，不回滚、不 raise——这类旧产物结构性缺少原始标签，无论重试多少次都
无法精确刷新，回滚只会让每次相同请求都白白重烧一次 LLM 推断 token；展示名会在
下一次触发完整处理（重新校对，天然产出带 `speaker_id` 的新 schema）时自然更新。
这与"刷新真正失败"（保存本身出错）是两种性质不同的分支：后者回滚+raise，前者
不算失败。

**已记录的设计决策（infer_speaker_names=false 的缓存复用语义，非缺陷，避免
后续 review 反复提出）**：`docs/sessions/260715-0635-pr3x/HANDOFF.md`（PR5
"说话人 artifact 与真实零 LLM"一节）原文——"infer_speaker_names 是独立开关，
默认 true"；"**false 时可复用完整有效映射；cache miss 只返回通用标签且不能
污染完成缓存**"。也就是说：`infer_speaker_names=False`（本轮用户显式关闭姓名
推断）时，`SpeakerInferencer.infer(allow_llm=False)` 若命中一份指纹匹配的
完整有效映射，应当直接复用，不因为 `allow_llm=False` 而拒绝已有的好结果；
只有真正 cache miss（没有可复用的有效映射）时才退回通用标签占位，且这份
占位结果不能写入/污染已完成的映射缓存——不能让一次"跳过推断"的占位结果
冒充真实推断结果持久化下来，污染后续相同指纹的请求。这是 PR5 设计阶段已
拍板的产品语义，不是实现遗漏，评审若再报按设计驳回，不需要重新讨论。

## 最终状态（2026-07-18）

PR3 加固分支（`pr3x/final`）的 8 轮分诊制 review 循环完成：每一轮对新增
发现按 P1（正确性 bug / 安全越权 / 数据丢失损坏，发现即修，不进
backlog）与 P2/P3（可接受，评估触发条件与影响范围后显式记录理由，不
阻塞发布）分诊，循环至无新增 P1 为止。累计 24 条 P1 发现，全部 TDD
红绿验证后修复（本文档前面各节的表格 + 补充轮记录覆盖其中大部分；本
分支后续几轮本地/CI codex review 的增量修复散见于各自 commit message，
未逐条重复誊录进本文档表格）。

最后一批 3 条 P1（Z1/Z2/Z3，gate 决策后发现，均为深层撤销/清理语义
问题，不属于表层可见 bug）：

| 编号 | 发现 | 修复 commit |
|---|---|---|
| Z1 | `check_view_token_ownership` 已撤销任务的候选被误判为"归属未知的纯 legacy 任务"，经历史提交审计行兜底重新拿到摘要/recalibrate 授权，撤销单调性被绕过 | `e2b59be` |
| Z2 | `cleanup_task_status` 的 `expire_task_snapshot` 调用成功后无条件置 `expired_this_attempt=True`，不区分"本轮真转换"与"早已撤销的空操作"；早已撤销 + 本轮删除失败时，失败补偿会错误复活 view_token | `0a84523` |
| Z3 | rmtree 失败留下的孤儿目录路径确定性、会被同一 (platform, media_id) 复用；save_cache 建新行时不清理残留旧格式产物，新行可能读到旧 funasr/LLM 混合内容 | `799392f` |

按用户方案 B 决策：这三条 P1 修复完成后，不再安排额外评审轮兜底，
直接进入真机冒烟测试。

Backlog（`BACKLOG.md`）累计 16 条接受记录（14 条 P2/P3 + 本轮新增 2
条：`correctness-speaker-confidence-threshold-precheck-bypass` P2、
`correctness-speaker-fingerprint-missing-label-drift` P3），均附带
接受理由与后续重新评估的触发条件，不阻塞当前发布。

## 真机端到端冒烟（2026-07-18，方案 B 完成项）

冒烟环境：本机真实后端（FunASR/CapsWriter WS、media_resolver、DeepSeek LLM 全真），存储/审计/日志/端口隔离至临时目录，通知渠道禁用。三条核心路径全部通过：

1. **冷启动链路**：`--check-config` 预检 → 启动 → `/livez` 200、`/health` healthy → SIGTERM 优雅关闭 254ms 退出，关闭清算零残留，全程日志零错误。
2. **提交→完成→公开查看**：真实 Bilibili 视频（BV1qt4y1X7TW）经 BBDown 下载 + FunASR 说话人转录 + LLM 校对/总结/姓名推断全流程 success；产物齐备（llm_status calibration=full 1/1、speaker_mapping 含指纹/schema/source=llm、dialogs 同时含 speaker_id 与展示名，LLM 正确推断出 UP 主真名）；view 页无认证 200 且内容完整。
3. **缓存与终态**：同 URL 重提交 2.4s success 且共享同一 view_token（零 LLM 重烧）；recalibrate 经归属校验放行、真实重校对 success。

### 部署迁移注记（预检对真实生产配置的两条拒绝，上线前需处理）

- 真实 config.jsonc 需**新增** `concurrent.llm_max_workers`（本分支新必需键，预检会拦截缺失）。
- 真实 config.jsonc 需**删除** `capswriter.path`（无任何代码消费的死键，严格校验会拒绝）。
