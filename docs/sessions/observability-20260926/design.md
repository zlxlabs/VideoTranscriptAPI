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

1. [实测] task_status 的终态快照仍由 `CacheManager.update_task_status` 的 CAS/write-once 路径写入；笔记成功来自 `llm_ops._handle_notes_generation`，笔记失败仅在 `_handle_llm_task` 的 notes-only 分支失败后标记。测试：`tests/integration/test_task_observability.py` 验证 success/failure producer payload 与持久快照；`tests/integration/test_immutable_task_snapshot.py` 锁住既有 write-once 行为。
2. [实测] audit 只持久化任务创建/完成时间和白名单观测 JSON，不复制 terminal_snapshot、URL 或错误正文；旧 audit 记录新增列为 NULL，不回填当前媒体缓存状态。代码：`audit_logger.py::_migrate_v6`、`_write_task_snapshot`；测试：`tests/unit/test_audit_snapshots.py` 与 `tests/integration/test_task_observability.py`。
3. [实测] CLI 以 audit 快照为主、按 task_id 合并 cache 补充，状态统计每个任务最多计一次；`created_at` 缺失保持 unknown，SQLite 只读连接不迁移。代码：`scripts/task_observability_report.py`；测试：`tests/unit/test_task_observability_report.py` 覆盖去重/旧 schema/空窗口/坏库/不创建路径。
4. [实测] UTC 输入必须带时区，窗口左闭右开；end-to-end 使用 created_at 到 completed_at，阶段分布使用已完成 tracker 区间，LLM duration_sum 使用 llm_usage 调用耗时并与墙钟分列。代码：`scripts/task_observability_report.py`、`PerfTracker.observation`；测试：`tests/unit/test_task_observability_report.py` 与 `tests/integration/test_task_observability.py`。
5. [实测] SQL 值全部绑定；输出只有白名单聚合，失败的 SQLite/schema/权限操作非零退出。代码：`scripts/task_observability_report.py`；测试：`tests/unit/test_task_observability_report.py` 的注入字符串阴性样例、权限/坏库用例及 CLI stdout 检查。
6. [实测] 正常终态和启动归档修复都调用同一 audit snapshot writer；cache 清理后报告仍由 audit 记录提供。代码：`CacheManager.update_task_status`、`AuditLogger.repair_task_snapshots`、`_write_task_snapshot`；测试：`tests/integration/test_task_observability.py`。

## 待验证前提

1. [推断] 生产 task_status.created_at 与 SQLite CURRENT_TIMESTAMP 均按 UTC 解释；上线只读配方将用数据库样例核对时间字段，不依赖主机本地时区。
2. [推断] 本轮历史 audit 快照缺 created_at/notes/perf 时无法倒推；报告将把可查询的缺字段计数与已知值分开，后续窗口不回填。
3. [推断] 后续 B 产物来源版本与 C 质量样本需要各自明确证据采集入口与保留策略；本轮先交付 A 观测闭环，不为未定数据新增字段。

## 验收路径

1. 入口：`python scripts/task_observability_report.py --cache-db PATH --audit-db PATH --since 2026-08-27T03:25:08Z --until 2026-09-26T03:25:08Z`。
2. 步骤：在副本 SQLite 上跑 producer 集成 fixture，创建成功、部分阶段失败和 notes 失败任务；调用 CLI 子进程读取两个数据库。
3. 预期：stdout 为白名单 JSON；任务去重且窗口按 `[since, until)`；阶段失败保留；端到端毫秒与并行 LLM 调用耗时分别统计；无权限、坏库、未知 schema 返回非零且不打印伪空结果。
