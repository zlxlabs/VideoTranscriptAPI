# PR3 加固分支 — P2 遗留问题 Backlog

> 时间：2026-07-16
> 分支：`pr3x/final`
> 关联：`docs/sessions/260716-pr3x-gate/REVIEW-LOG.md`（本轮 gate review 全量记录）

## 分诊规则

- **P1（必修）**：正确性 bug、安全/越权漏洞、数据丢失/损坏——发现即修，不进
  backlog。
- **P2/P3（可接受）**：其余问题（可用性降级、边界场景、性能/精度退化等）——评估
  触发条件与影响范围后，可以显式记录为"接受不修"，附带接受理由与后续重新评估的
  触发条件，不阻塞当前发布。

## 本服务风险定位

个人 + 朋友使用规模的服务（非对外开放的多租户 SaaS）；处理量级低（预计 <50
任务/天）；转录/校对/总结产物通过 view_token 公网可访问（无强身份鉴权的分享
链接）。这个定位是下面每条"接受"判断的共同前提——评估的是"当前实际使用场景下
会不会被触发、触发后影响有多大"，而不是"理论上是否可能出问题"。风险定位发生
变化时（例如对外开放、接入更多用户、处理量级显著上升），必须重新评估以下每
一条。

## 本轮接受不修的 P2 决定

### 1. correctness-audit-repair-starves-missing-snapshots

- **严重度**：P2（correctness，持续高吞吐场景下会饥饿）
- **问题**：`repair_task_snapshots`（对同步归档失败的任务做周期性补录）的吞吐量
  有上限（约 500 条/24h）；如果同步归档失败的任务持续、大量产生，repair 补录
  速度追不上产生速度，缺失快照的任务会在队列里越攒越多，永远补不完。
- **接受理由**：个人使用规模（<50 任务/天）远低于 repair 的吞吐下限（500/24h），
  且同步归档才是快照落地的主路径（终态写入时的同步归档，见
  `CacheManager.update_task_status`），repair 只是它失败时的补录兜底，不是
  主要依赖路径。
- **触发条件重估**：持续（不是偶发）每天产生 >500 条同步归档失败——需要重新
  评估是否要提高 repair 吞吐上限，或者优先改进同步归档本身的可靠性。

### 2. correctness-audit-snapshot-retention-extends-on-task-cleanup

- **严重度**：P2（correctness，审计快照保留期语义轻微漂移）
- **问题**：任务被 re-archive（重新归档，例如 repair 补录到已存在快照的任务、
  或恢复流程重新写入）时，`archived_at` 会被刷新为当前时间，导致这条审计快照
  的实际保留期从"首次归档时间 + 保留窗口"被动延长到"最近一次 re-archive
  时间 + 保留窗口"。
- **接受理由**：保留期最多被延长一个任务生命周期的量级（re-archive 只发生在
  任务生命周期内的有限次数，不会无限延长），个人 + 朋友使用场景下数据敏感度
  低，审计快照多保留一段时间不构成实质风险。
- **触发条件重估**：若未来对外开放（本服务风险定位变化，见上），或需要满足
  严格的数据保留期合规要求，需要修复——修复方式很直接：re-archive 时保持
  `archived_at` 原值不刷新，只更新其余字段即可。

### 3. reliability-shutdown-temp-cleanup-wait-default-executor

- **严重度**：P2（reliability，关闭预算计时精度问题，非功能性故障）
- **问题**：优雅关闭（`RuntimeContext.aclose()`）流程里，等待临时文件清扫完成
  这一步用的是共享的默认线程池执行器排队，而不是有独立配额的专用执行器——在
  关闭预算的计时精度上引入了不确定性（排队等待时间不计入某个专属预算段，可能
  让实际关闭耗时超出预期）。
- **接受理由**：这个问题只影响关闭预算的计时精度，不影响关闭本身能否成功完成；
  本项目唯一支持的部署形态是容器化（D3 自动部署流水线），容器 `SIGTERM` →
  超时强制 `SIGKILL` 为进程退出提供了硬上界，与 REVIEW-LOG.md「已接受的设计
  决策」一节的既有决策 #1（"关闭超时不约束进程退出"——`aclose()` 的有界等待
  只保证 lifespan 本身不会无限悬挂，残余线程由容器终止兜底）是同一类风险的
  另一个实例，已经被那条决策的论证覆盖。
- **触发条件重估**：若关闭预算的计时精度本身成为可观测的运维问题（例如频繁
  触发 SIGKILL 而非优雅退出、或因此影响数据完整性），需要把清扫等待迁移到
  专用执行器。

### 4. reliability-shutdown-wait-queues-on-default-executor

- **严重度**：P2（reliability，关闭预算计时精度问题，非功能性故障）
- **问题**：`app.py:447`（`shutdown_event`）里 `await asyncio.to_thread(cleanup_done.wait, remaining)`——
  虽然临时目录清扫本体已经改在独立 daemon 线程里跑（不受事件循环默认执行器
  收尾逻辑管辖，见该处大段注释），但等待清扫完成这个"至多等 remaining 秒"
  的 `Event.wait` 调用本身仍然经由 `asyncio.to_thread` 提交给事件循环的
  **共享默认执行器**——如果默认执行器当时被其他任务占满，这次等待调用自身
  也会排队，同样会在关闭预算的计时精度上引入不确定性。
- **判定**：与第 3 条已接受项（`reliability-shutdown-temp-cleanup-wait-default-executor`）
  同根因复报——都是"排队等待共享默认执行器"这一类问题在关闭路径上的又一处
  实例，不是新的独立风险。维持接受，理由不变：本项目唯一支持的部署形态是
  容器化（D3 自动部署流水线），容器 `SIGTERM` → 超时强制 `SIGKILL` 为进程
  退出提供了硬上界，与 REVIEW-LOG.md「已接受的设计决策」一节的既有决策 #1
  同属一类风险，已被那条决策的论证覆盖。
- **触发条件重估**：与第 3 条相同——若关闭预算的计时精度本身成为可观测的
  运维问题，需要把这次等待也一并迁移到专用执行器，与清扫本体一起解决。

### 5. security-recalibrate-ownership-bypass-by-resubmission

- **严重度**：P3（security，设计边界内的产品语义，非漏洞）
- **问题**：`/api/recalibrate`（`tasks.py:464`）的归属校验（`tasks.py:510-536`
  一带）只核对发起者对目标 `view_token` 的权限，不核对目标媒体（URL 对应的
  `platform+media_id`）在缓存里是否原本由同一个提交者建立——媒体级缓存产物
  跨提交者复用是既有设计，任何具备 `recalibrate` 权限的认证用户，理论上都
  可以通过对一个自己从未提交过的 URL 发起新的 transcribe/recalibrate 请求，
  触发对该媒体已有产物的重新处理。
- **接受理由（已拍板的产品语义）**：媒体缓存与产物本就是跨提交者复用的——
  具备 `recalibrate` 权限的认证用户本来就可以对任意 URL 发起一次全新的
  处理请求（下载 + 转录 + 校对），重复提交并不会让这个用户获得他原本不具备
  的额外能力，只是复用了已有缓存而非重新算一遍。当前服务使用者范围是本人
  + 受信任的朋友，不存在"陌生用户滥用他人产物"的实际威胁模型。
- **触发条件重估**：若未来扩大开放范围（服务对外开放注册、允许不受信任的
  用户接入），需要重新评估——把写操作的权限判定从"是否具备 recalibrate
  权限"收紧到"按媒体级归属校验"（只有原始提交者或管理员能触发对某个媒体
  产物的重新处理），而不能继续依赖"用户群体互信"这个当前成立、未来可能不
  成立的前提。

### 6. reliability-shutdown-temp-cleanup-wait-uses-default-executor

- **严重度**：P2（reliability，关闭预算计时精度问题，非功能性故障）
- **问题**：本轮（第三次）review 再次命中 `app.py:447`（`shutdown_event`）
  的 `await asyncio.to_thread(cleanup_done.wait, remaining)`——临时目录清扫
  本体已经改到独立 daemon 线程里跑，但等待它完成这个"至多等 remaining 秒"
  的 `Event.wait` 调用本身，仍然经由事件循环的共享默认执行器提交，与第 3、
  4 条已接受项完全同一根因。
- **判定**：同根因第三次复报（同第 3、4 条），不是新的独立风险，维持接受，
  理由不变：本项目唯一支持的部署形态是容器化（D3 自动部署流水线），容器
  `SIGTERM` → 超时强制 `SIGKILL` 为进程退出提供了硬上界，与 REVIEW-LOG.md
  「已接受的设计决策」一节的既有决策 #1 同属一类风险，已被那条决策的论证
  覆盖。
- **触发条件重估**：与第 3、4 条相同——若关闭预算的计时精度本身成为可观测
  的运维问题，需要把这次等待也一并迁移到专用执行器，与清扫本体、
  `_stop_workers`（已经历同类修复，见 `context.py::_stop_workers_off_shared_pool`，
  本地 codex review 第 16 轮 Q5）统一处理。

### 7. correctness-speaker-mapping-display-nonatomic-concurrent-commit

- **严重度**：P2（correctness，短暂展示不一致，具备自愈能力）
- **问题**：说话人映射（`speaker_mapping.json`，权威存储）与转录展示（结构化
  产物内嵌的 `dialogs` 展示姓名）是两次独立的落盘提交——`SpeakerInferencer.
  infer()` 先在一次 `media_lock` 临界区内写入新的 `speaker_mapping.json`，
  随后展示层的刷新（`llm_ops._refresh_speaker_names_in_existing_structured_
  artifact` / `_save_llm_results` 的结构化保存）在另一次单独的 `media_lock`
  临界区内才把 `dialogs` 展示姓名同步过去（见 `llm_ops.py:789` 起的函数
  文档）。两次提交之间没有跨临界区的原子性：若同一媒体存在并发任务（例如
  两个用户几乎同时首次请求同一个视频，触发两次独立的 LLM 处理），两个
  任务各自的"写 mapping -> 写展示"两步操作可能交错提交，短暂出现 mapping
  与展示不对应的组合（如 mapping 已是任务 B 的新结果，展示仍停留在任务 A
  的旧姓名）。
- **接受理由**：
  1. cache_hit 一致性自愈（第 7 轮 codex review，H6，`llm_ops.py:833` 起）
     会在后续任意一次命中该媒体缓存的请求中，按每条 dialog 的 `speaker_id`
     逐条比对权威 mapping 与展示姓名，一旦发现分叉即自动刷新纠正，零 LLM
     成本，把不一致窗口收敛到"下一次请求"为止，不会永久存留；
  2. 同一媒体被两个及以上任务并发首次处理，在个人 + 朋友使用规模（<50
     任务/天，多数请求命中缓存）下概率极低，几乎不构成实际风险；
  3. 彻底消除这个窗口需要把"写 mapping"和"写展示"合并进跨阶段的单一媒体级
     事务（例如让两次提交共享同一次 `media_lock` 临界区，或引入版本号/
     乐观锁协调跨任务写序），这是当前锁池设计之外的新机制，与本轮"做减法、
     只用既有机制"的修复纪律冲突，不在本轮范围内引入。
- **触发条件重估**：并发量显著上升（服务对外开放、用户规模扩大导致同媒体
  并发首次处理不再是小概率事件），或者上面依赖的 cache_hit 自愈机制
  （H6）被后续改动移除/弱化，需要重新评估并实现媒体级事务化提交。

### 8. correctness-cache-cleanup-task-admission-race

- **严重度**：P2（correctness，锁内双复核后残余的微秒级 TOCTOU 窗口）
- **问题**：`cleanup_old_cache` 本轮（S1 同批次）已修复为逐条抢占媒体锁 +
  锁内复核在途任务（`5914f6f`），堵住了清理与任务写入之间的大部分竞争
  窗口。但"复核在途任务是否存在"与"清理真正执行删除"之间仍然隔着锁内
  两次独立的数据库读写操作，不是单一原子事务——理论上仍存在一个微秒级
  窗口：复核那一刻确认"无在途任务"，紧接着（复核之后、删除之前）一个新
  任务针对同一媒体完成准入（写入 `task_status` 行），随后清理沿用复核
  时的"无在途"结论继续删除，与这个刚准入的新任务产生竞争。
- **接受理由**：后果有界且可自愈——单任务失败可重试（客户端/用户重新发起
  一次请求即可，`_save_llm_results`/`transcription.py` 的分层缓存判定
  会把"缺文件"当未确认完成正确触发重试），媒体本身可重新下载/转录，不
  产生数据损坏或跨用户越权，只是一次性的"这次请求恰好撞上清理，需要重
  跑一次"。彻底闭合这个窗口需要让"任务准入"（`create_task`/写
  `task_status` 首行）也持有同一把媒体锁，把"复核在途 -> 删除"与"任务
  准入"合并进单一临界区——这会在任务创建这条服务全局最热的路径上引入
  与清理任务共享的锁竞争（后台周期清理与前台每一次用户请求的准入路径
  争用同一把锁），新增的耦合/性能代价与这个窗口本身极低的实际触发概率
  （微秒级窗口 + 个人使用规模的低并发量）不成比例，不值当。
- **触发条件重估**：清理任务与任务创建的并发频率显著上升（例如清理周期
  被调得极短，或任务提交速率大幅提高），使这个微秒级窗口的实际命中概率
  从"理论存在"变为"可观测复现"，需要重新评估是否要把任务准入纳入媒体锁
  临界区。

### 9. correctness-speaker-mapping-standalone-validation-bypass

- **严重度**：P3（correctness，仅存在于非生产接线路径）
- **问题**：`LLMCoordinator.__init__`（`llm/coordinator.py:27`）的
  `media_cache_manager` 参数默认 `None`，此时说话人推断器
  （`SpeakerInferencer`）会退回使用 `self.cache_manager`——协调器内部
  独立实例化的 `llm/core/cache_manager.CacheManager`，与顶层
  `cache/cache_manager.CacheManager` 是两个完全独立的实现，前者的
  `get_speaker_mapping`/`save_speaker_mapping` 没有接入 R5 那一批"读写两侧
  共用同一份深层形状校验"（`_speaker_mapping_result_is_valid`）的加固——
  独立协调器路径存在写得进、读不出，或读到形状不合法数据直接抛异常的
  理论风险。
- **接受理由**：这条路径在生产环境不可达。唯一的真实构造点
  `api/context.py:885`（`RuntimeContext.start()`）总是显式传入
  `media_cache_manager=self.cache_manager`（顶层、已加固的实现），
  `LLMCoordinator()` 不带这个参数构造只会出现在测试或未来可能新增的
  脚本化/独立调用场景。与既有已接受项同一类判断（生产接线路径已经用
  正确的依赖注入堵住风险面，参数默认值本身的薄弱只在非生产路径才会
  暴露），当前不需要为一条生产不可达的路径同步加固。
- **触发条件重估**：新增任何在生产链路上不经 `api/context.py` 构造
  `LLMCoordinator` 的调用点（例如独立 CLI 工具、脚本化批处理任务）时，
  必须显式传入顶层 `cache_manager` 作为 `media_cache_manager`，否则需要
  把 R5 的深层校验同步补到 `llm/core/cache_manager.py`。

### 10. correctness-history-task-dedup

- **严重度**：P3（correctness，防御性加固，当前无可触发路径）
- **问题**：`/api/audit/history` 相关查询未对 `task_id` 做 `DISTINCT`
  去重——若底层查询在某种条件下对同一个 `task_id` 产生多条记录，历史
  列表会重复展示同一个任务。
- **接受理由**：审计梳理未找到任何会为同一个 `task_id` 产生多条提交日志
  行的代码路径——每一次真实提交（`create_task`）都会生成一个全新的
  `task_id`（UUID，见 `CacheManager.generate_task_id`），且每次请求只
  写入一行 `task_status` 记录，没有"同一 `task_id` 多次写入历史表"的
  重试/补偿逻辑。防御性 `DISTINCT` 在当前代码路径下属于加固冗余
  （anticipated but not actually reachable），非必需变更。
- **触发条件重估**：未来若引入任何会对同一 `task_id` 重复写入历史记录的
  机制（如提交重试补偿、跨进程去重失败后的补录），需要重新评估并补上
  `DISTINCT`（或等价的查询层去重）。

### 11. correctness-legacy-structured-divergence-bypasses-rebuild

- **严重度**：P2（correctness，展示姓名陈旧，具备可绕过的自愈路径）
- **问题**：`structured_dialogs_consistent_with_mapping` /
  `_refresh_speaker_names_in_existing_structured_artifact` 的"旧格式兼容"
  立场——完全没有 `speaker_id` 的 legacy 结构化产物（main 时代、本轮
  speaker_id schema 迁移之前生产的存量数据）没有原始标签可以精确核验，
  一律按"可信一致"处理，不判定为分叉、不触发刷新。这类 legacy 产物的
  展示姓名会一直保持写入时点的值，即使顶层权威 `speaker_mapping.json`
  后续已经变化（例如同一媒体后来又被重新推断出更准确的姓名），也不会
  自动同步，直到该媒体下一次触发完整重处理（重新校对/重新说话人推断）
  重新生成新 schema 的产物为止。
- **接受理由**：这是本 PR 引入的 speaker_id 精确核验机制刻意保留的边界，
  不是遗漏——没有 speaker_id 就没有安全的核验依据，强行按显示名反查会
  重新引入"两个原始说话人共享同一旧显示名时张冠李戴"的数据损坏风险（见
  `_refresh_speaker_names_in_existing_structured_artifact` 本轮修复的
  同类问题）。若改为"顶层映射一旦分叉就强制重建"，会让每一份受影响的
  legacy 产物在被访问/复核时都烧一次完整 LLM 重处理——个人 + 朋友使用
  规模（<50 任务/天）下，为了修正一份纯展示层面、多数情况下无人察觉的
  姓名陈旧，重新支付一整次转录后处理的 token 成本，收益与代价不成比例。
- **触发条件重估**：用户实际报告某个公开分享页面的说话人姓名明显陈旧
  （例如与最近一次已知的正确推断结果不符），需要重新评估——落地方式
  可以是为 legacy 产物单独设计一次性的迁移补丁（不依赖运行时按访问触发
  重建），而不是在热路径上无差别地对所有 legacy 产物做强制重建。

### 12. correctness-legacy-attribution-retention-gap

- **严重度**：P2（correctness，历史列表展示降级，快照与直访不受影响）
- **问题**：legacy（本轮迁移前）任务的归属证据依赖审计日志（`audit.db` 的
  提交类端点日志行，见上面第 2768a15 条归属改锚定 `submitted_by` 的
  修复），而审计日志本身有保留期、到期会被清理——迁移前产生的这批任务，
  它们的归属证据会随审计日志保留期到期而自然衰减/消失。归属证据一旦
  缺失，这些任务在"我的历史"列表（`/api/audit/history`）里会因通不过
  归属校验而不再展示。
- **接受理由**：影响面严格限定在"历史列表展示"这一层——
  1. 快照数据本身不受影响：`task_audit_snapshots` 的归档快照（内容/
     产物）不依赖审计日志保留，不会随之丢失或损坏；
  2. `view_token` 直接访问不受影响：查看页鉴权走的是 `view_token` 本身
     （分享链接模型），不经过归属校验这条路径，历史列表看不到不等于
     分享链接失效；
  3. 只有"回到历史列表里找到这条任务"这一个使用路径会受影响，且仅限于
     迁移前的存量任务（新任务走 `submitted_by` 直接锚定，不受审计日志
     保留期影响）；
  4. 富化方案是现成的、非阻塞的：可以写一次 repair 脚本，把 legacy 任务
     的归属（如果能从其它信源如访问日志/人工记录中找回）回填进
     `task_audit_snapshots` 的 legacy 快照，一次性补齐，不需要现在就做。
- **触发条件重估**：用户实际反馈"历史列表里找不到某个迁移前提交的任务"，
  或需要长期保留 legacy 任务可查询性（例如合规/审计需要），再实施上面
  第 4 点的 repair 回填富化方案；不需要现在预先实现。

### 13. stale-speaker-mapping-reuse（第三变体：指纹基于共享缓存快照而非任务不可变输入）

- **严重度**：P2（correctness，短暂展示不一致，与既有接受项 #7 同族同前提）
- **问题**：`SpeakerInferencer.input_fingerprint(speakers, dialogs)`（`llm/core/
  speaker_inferencer.py`）在每次调用 `infer()` 时按传入的 `speakers`/`dialogs`
  现算一次指纹，而不是绑定某个任务在创建之初就已经固化下来的不可变输入
  快照——调用方（`transcription.py` 的缓存命中分支、`llm_ops.py` 的补层
  刷新分支）传入的 `dialogs` 来自当次请求现读的共享缓存（`cache_data
  ["transcript_data"]`），本质是这份共享媒体缓存"此刻恰好是什么样"的一次
  快照，而不是这个任务自己不可变的原始输入。若同一媒体存在并发任务（与
  第 7 条 `correctness-speaker-mapping-display-nonatomic-concurrent-commit`
  同一前提：例如两个用户几乎同时首次请求同一视频，或一个请求与一次并发
  的 recalibrate/重新转录交错），任务 A 现读到的 `dialogs` 快照可能已经是
  任务 B 部分写入过程中的中间态，A 据此算出的指纹既不严格对应 A 自己发起
  请求时的原始输入，也不对应任何一个任务最终稳定落盘的版本，`save_speaker_
  mapping` 落盘后可能与随后任何一次干净读取（干净地反映 A 或 B 各自完整
  产物）的指纹都对不上，被当作缓存未命中重新触发一次推断。
- **接受理由**：
  1. 前提与第 7 条完全相同（同媒体并发首次处理），个人 + 朋友使用规模
     （<50 任务/天）下概率极低，不构成实际风险；
  2. 后果有界且自愈——最坏情况是指纹不匹配导致的一次多余重新推断（浪费
     一次 LLM 调用），不会产生数据损坏、张冠李戴或跨用户越权，且 `get_
     speaker_mapping` 的读侧深层校验（R5/Y3）保证任何写入过的内容形状
     必然合法，"重新推断一次"是唯一可能的代价；
  3. 彻底修复需要把"任务自己的不可变输入"（发起这次处理时的 dialogs/
     speakers 快照）穿透进 `input_fingerprint` 与 `save_speaker_mapping`
     调用链——即从"调用时现读共享缓存"改为"任务级别固化输入、全程携带
     不可变副本"，这是跨越 `transcription.py`/`llm_ops.py`/
     `speaker_inferencer.py` 三层的接口改造（新增任务级不可变快照的传递
     机制），不是本轮"做减法、复用既有机制"的范围。
- **触发条件重估**：与第 7 条相同——并发量显著上升（服务对外开放、用户
  规模扩大导致同媒体并发首次处理不再是小概率事件），需要与第 7 条一并
  重新评估，实现任务级不可变输入穿透进保存层。

### 14. correctness-cache-bundle-partial-commit

- **严重度**：P2（correctness，崩溃窗口内的产物混合态，下次完整重处理自愈）
- **问题**：一次转录/校对/总结处理会依次落盘一组产物文件（转录文本、
  `llm_calibrated.txt`、`llm_summary.txt`、`llm_processed.json`、
  `speaker_mapping.json`、`llm_status.json` 等）——每一个单文件的写入
  本身是原子的（`_atomic_write`：先写临时文件再 `os.replace`，见
  `cache_manager.py`），`video_cache`/`task_status` 的 DB 行提交在这组
  文件写完之后发生，充当整组产物"已完成"的弱提交点（`llm_status.json`
  是这组产物里最后写入的文件，见 S1 修复的 write-ahead 设计）。但这组
  文件作为一个整体（bundle）没有跨文件的原子性——如果进程在写完文件 N、
  还没写到文件 N+1（例如 `llm_calibrated.txt` 已落盘，`llm_summary.txt`
  还没来得及写）时崩溃（OOM kill、宿主机断电、容器被强杀等），磁盘上会
  留下一份"部分新、部分旧/缺失"的混合态产物组。
- **接受理由**：
  1. 单文件原子性已经堵住了"半个文件"这一层风险（不会出现内容被截断
     一半的损坏文件，只会出现"整份文件要么是新的、要么完全不存在/是旧
     的"）；
  2. `llm_status.json` 作为组内最后写入的文件，天然是"这组产物是否完整
     提交"的弱标记——分层缓存判定（`transcription.py` 的 `calibrated_
     layer_satisfied`/`need_summary` 等）已经按"状态文件缺失一律视为未
     确认完成"处理，下一次针对同一媒体的请求（或 `/api/recalibrate`）
     会自然触发对缺失层的重新处理，把混合态收敛为完整态，不需要人工
     介入；
  3. 把整组产物变成真正的跨文件事务（bundle 级提交）是一种新机制（例如
     写入一份 manifest 文件、或先全部写临时路径再一次性 rename 整个
     目录），与本轮"做减法、只用既有机制"的修复纪律冲突，不在本轮范围
     内引入；
  4. 崩溃发生在写产物文件期间这个窗口本身很窄（相对整个任务生命周期），
     个人 + 朋友使用规模下没有实际观测到过因此导致的可见问题。
- **触发条件重估**：观测到真实的崩溃驱动的产物混合态（例如用户报告某个
  分享页面同时呈现"新校对文本 + 旧总结"这类不一致组合，且排除了本轮
  已修复的 llm_status 半提交场景），或部署环境从"稳定容器化，异常终止
  概率低"变为"频繁被抢占式终止"（如迁移到 spot/抢占式实例），需要重新
  评估是否要为产物 bundle 引入跨文件事务化提交。

### 15. correctness-speaker-confidence-threshold-precheck-bypass

- **严重度**：P2（correctness，配置变更不能即时生效，具备后续触发路径自愈）
- **问题**：`transcription.py:857`（`need_speaker_names = current_speaker_
  mapping is None`）是决定是否需要重新走一遍说话人推断层的分层缓存预检——
  只要 `cache_manager.get_speaker_mapping(..., input_fingerprint=fingerprint,
  speakers=speakers)` 按 fingerprint 命中一份完整映射，`need_speaker_names`
  即为 `False`，本轮请求整个跳过 `SpeakerInferencer.infer()`，直接复用缓存
  展示产物。但 `infer()` 内部真正按当前 `confidence_threshold` 重新门控
  （`_apply_confidence_gate`，见 `speaker_inferencer.py:259` 起，用缓存 meta
  里保留的原始 name/confidence、当前阈值重判是否采用推断姓名）的逻辑只有在
  `infer()` 真正被调用时才会执行——被这条预检直接跳过时，缓存展示产物仍然
  是写入那一刻的旧阈值门控结果，用户调整 `confidence_threshold` 配置（调高
  收紧或调低放宽）后，已有缓存映射不会立刻反映新阈值，除非该媒体因为其它
  原因（fingerprint 变化、映射不完整覆盖当前 speakers 等）触发了完整重推断。
- **接受理由**：与 REVIEW-LOG.md「已接受的设计决策」一节的既有决策"speaker
  artifact 指纹不含 metadata_override/模型切换"是同一决策家族——`input_
  fingerprint` 本就只由 `speakers`/`dialogs`（媒体内容本身）决定是否可复用，
  配置层参数（无论是模型版本还是置信度阈值）的变更都不参与指纹计算、不
  自动触发失效重判，这是刻意取舍：配置调整不应让全部历史映射作废、逼迫
  重烧 token 全量重新推断。若确实需要"这条媒体按新阈值重新门控"，应该走
  显式失效入口（如已存在的 `CacheManager.invalidate_speaker_mapping`，删除
  后下一次请求视为缓存未命中），而不是让阈值参与自动失效判断。
- **触发条件重估**：用户实际反馈"调整了置信度阈值配置，但某个已缓存媒体的
  展示姓名没有变化"且需要立即生效（而非等待下一次自然触发的完整重推断），
  需要评估补一个显式的"按当前阈值重新门控存量映射"管理入口（复用 `_apply_
  confidence_gate` + 缓存 meta 里的 name/confidence，纯本地重判、零 LLM
  成本，不需要真实重新推断）。

### 16. correctness-speaker-fingerprint-missing-label-drift

- **严重度**：P3（correctness，读写侧口径理论分叉，FunASR 契约下不可达）
- **问题**：说话人标签提取的读侧与写侧对"完全缺失说话人字段"的 dialog 处理
  口径不一致。读侧 `SpeakerInferencer.extract_speaker_labels`（`speaker_
  inferencer.py:63` 起，供 `transcription.py` 分层缓存预检计算 `speakers`/
  `input_fingerprint` 用）在 `resolve_dialog_speaker(item) is None`（`speaker`/
  `spk`/`speaker_id` 三个别名字段都不存在）时，直接跳过该 dialog、不贡献
  任何标签进 `speakers` 集合。写侧 `SpeakerAwareProcessor._coerce_dialogs`
  （`speaker_aware_processor.py:220` 起）对同样"speaker 字段缺失"的 dialog
  不跳过，而是把它规范化为字面量 `"speaker": "unknown"` 并保留在 `base_
  dialogs` 里，下游据此推导的 speakers 集合会多出一个 `"unknown"` 成员——
  两侧对同一份输入算出的 speakers 集合可能不同，导致 `input_fingerprint`
  分叉，与已修复的 `5917aa9`（`spk=0` 被错误折叠成 `"unknown"`）是同一族
  问题的另一变体：那条修的是"falsy 但合法"（数值 0）的误判，这条是"标签
  字段确实完全不存在"的读写侧跳过策略不一致。
- **接受理由**：`resolve_dialog_speaker` 遍历的三个别名字段（`speaker`/
  `spk`/`speaker_id`）覆盖了本项目实际 ASR 提供方（FunASR 说话人分离输出）
  的完整字段契约——每一条 diarization 分段结果都必然带有说话人标识字段，
  "三个别名字段都不存在"是当前数据管道不会产生的退化输入，只有手工构造的
  畸形测试数据或未来接入新 ASR provider 且其输出字段完全不在别名链覆盖
  范围内时才会真正触发。评审对本条问题的严重度自判为 minor（存在但不可达
  的读写侧口径差异），不阻塞本轮。
- **触发条件重估**：接入新的 ASR/diarization provider 且其原始输出的说话人
  字段命名不在 `resolve_dialog_speaker` 当前遍历的别名链内，需要同步评估
  是否要把"缺标签"也当作 `"unknown"` 统一读写两侧口径（让读侧 `extract_
  speaker_labels` 与写侧 `_coerce_dialogs` 一样把缺标签 dialog 保留为
  `"unknown"` 而不是跳过），或者在接入新 provider 时把新字段名补进别名链
  从根上避免"缺标签"场景出现。
