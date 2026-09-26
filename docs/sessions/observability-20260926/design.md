# DESIGN-note：为任务终态补齐可复盘的耗时与用量

## 目标

用户可用一条只读命令按 UTC 创建窗口复盘任务终态、笔记/总结/章节状态、阶段耗时和 LLM 用量，且清理任务缓存后审计快照仍可查询。

## 非目标

不替换队列、不增依赖或服务、不重试、不加 UI、不改下载器、不做章节 checkpoint、不部署生产；产物来源版本与内容质量评估留给后续增量。

## 为什么不是分区 / 删除 / 约定

- **分区**：任务终态和 LLM 用量已经分别由 cache SQLite 与 audit SQLite 持有；再建库或状态表只会增加跨库同步点，不能消除这两个既有边界。
- **删除**：只读复盘、笔记成功/失败状态与阶段耗时是本卡目标，删除任一项都会使“结果与阶段耗时”无法回答；不可靠的历史起点则明确记缺失，不猜测。
- **约定**：进程内日志和媒体缓存会轮转或清理，不能保证任务记录长期存在；需由现有终态归档写入审计库，CLI 才能在清理后复盘。

## 方案要点与已否决方案

- **要点**：沿用 task_status 的 write-once terminal_snapshot 和 audit task_audit_snapshots；审计库新增 nullable created_at 与白名单 observability_json。终态快照持久化 PerfTracker 已完成阶段汇总、详细笔记状态；LLM-only worker 成功和失败都显式写入 notes_status。只读 CLI 以 audit 为主、cache 为现存字段补充、按 task_id 去重；用 created_at 选择 `[since, until)` UTC 任务，用 created_at/completed_at 算墙钟，用 llm_usage 单列累计调用耗时与 token。
- **已否决**：从共享 llm_status/media cache 回填旧任务 notes 状态会把“现在产物存在”误当成“该任务曾生成”；从 API 请求审计时间猜任务创建时间会错配任务；复制完整 terminal_snapshot 会将 URL 与原始异常正文带入审计观测；用 PerfTracker 阶段耗时总和代表墙钟会把并行阶段重复计时。

## 关键不变式

1. [实测] task_status 的终态快照仍由 `CacheManager.update_task_status` 的 CAS/write-once 路径写入；笔记状态表示本轮产物完整性，仅在笔记文件和媒体状态都写入后记为 generated，随后终态写失败不会覆盖它；生成/保存阶段失败仍写 failed。代码：`llm_ops._handle_notes_generation`、`_handle_llm_task`；测试：`test_notes_terminal_exception_preserves_generated_artifact_and_snapshot`（CAS 前/后异常参数化）、`test_notes_worker_snapshot_survives_cache_cleanup_and_cli_reports_real_failure`、`tests/integration/test_immutable_task_snapshot.py`。
2. [实测] audit 只持久化任务创建/完成时间和白名单观测 JSON，不复制 terminal_snapshot、URL 或错误正文；旧 audit 记录新增列为 NULL，不回填当前媒体缓存状态。代码：`audit_logger.py::_migrate_v6`、`_write_task_snapshot`；测试：`tests/unit/test_audit_snapshots.py` 与 `tests/integration/test_task_observability.py`。
3. [实测] CLI 完整读取 audit/cache，先按 task_id 合并再按合并后的有效 created_at 选择 UTC 窗口；有效 audit 时间优先，audit 时间缺失/无效时采用有效 cache 时间，两者均无效才计一次未归属。任务窗口内最多计一次。`schema_version` 必须恰有一行整数5或6；v5保留原必需列，v6另需 `created_at` 与 `observability_json`，不迁移数据库。`fields_missing.task_status` 等于 `tasks.status_counts.unknown`，success、failed、queued、processing、calibrating 为已知状态。代码：`scripts/task_observability_report.py`；测试：`tests/unit/test_task_observability_report.py` 覆盖版本/列参数化拒绝、窗口来源冲突/无效起点矩阵、未知状态、去重、旧 schema 及空窗口。
4. [实测] UTC 输入必须带时区，窗口左闭右开；end-to-end 使用 created_at 到 completed_at，阶段分布使用已完成 tracker 区间，LLM duration_sum 使用 llm_usage 调用耗时并与墙钟分列。代码：`scripts/task_observability_report.py`、`PerfTracker.observation`；测试：`tests/unit/test_task_observability_report.py` 与 `tests/integration/test_task_observability.py`。
5. [实测] SQL 值全部绑定；输出只有白名单聚合，失败的 SQLite/schema/权限操作非零退出。代码：`scripts/task_observability_report.py`；测试：`tests/unit/test_task_observability_report.py` 的注入字符串阴性样例、权限/坏库用例及 CLI stdout 检查。
6. [实测] 正常终态和启动归档修复都调用同一 audit snapshot writer；cache 清理后报告仍由 audit 记录提供。代码：`CacheManager.update_task_status`、`AuditLogger.repair_task_snapshots`、`_write_task_snapshot`；测试：`tests/integration/test_task_observability.py`。
7. [实测] PerfTracker 只将 `cache_hit`、`cache_hit_partial` 两个非负计数器纳入观测与审计白名单；`unknown` 表示两者都没有正计数，full 与 partial 可同时命中且各自计数，不能相加当作任务总数。full hit 在终态快照前计数。代码：`PerfTracker.observation`、`AuditLogger._write_task_snapshot`、`scripts/task_observability_report.py`、`transcription.process_transcription`；测试：`test_observation_allowlists_cache_hit_counters`、`test_full_cache_hit_is_persisted_and_reported_by_cli`、`test_task_status_and_cache_hit_counters`。
8. [实测] metadata 只有实际发起探测时才进入 tracker；普通探测异常在区间退出后按既有兜底继续，失败阶段仍保留。转录缓存保存失败先离开 track，再调用原有终态失败收口。代码：`transcription.process_transcription`；测试：`test_transcription_stage_outcomes_are_persisted`（metadata/no-probe/CapsWriter/FunASR 参数化）。

## 待验证前提

1. [推断] 生产 task_status.created_at 与 SQLite CURRENT_TIMESTAMP 均按 UTC 解释；上线只读配方将用数据库样例核对时间字段，不依赖主机本地时区。
2. [推断] 本轮历史 audit 快照缺 created_at/notes/perf 时无法倒推；无 created_at 的历史行只在窗口外单独计数，不计入任务状态或窗口缺字段分母。窗口内快照缺 notes/perf 时显式记 unknown/missing，不回填共享媒体状态。
3. [推断] 后续 B 产物来源版本与 C 质量样本需要各自明确证据采集入口与保留策略；本轮先交付 A 观测闭环，不为未定数据新增字段。

## 验收路径

1. 入口：`python scripts/task_observability_report.py --cache-db PATH --audit-db PATH --since 2026-08-27T03:25:08Z --until 2026-09-26T03:25:08Z`。
2. 步骤：在副本 SQLite 上跑 producer 集成 fixture，创建成功、部分阶段失败和 notes 失败任务；调用 CLI 子进程读取两个数据库。
3. 预期：stdout 为白名单 JSON；任务去重且窗口按 `[since, until)`；阶段失败保留；端到端毫秒与并行 LLM 调用耗时分别统计；无权限、坏库、未知 schema 返回非零且不打印伪空结果。
