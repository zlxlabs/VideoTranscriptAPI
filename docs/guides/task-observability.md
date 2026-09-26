# 任务结果与耗时复盘

## 本地只读命令

在仓库 checkout 中执行；两个数据库都以 SQLite `mode=ro` 打开，CLI 不迁移 schema，也不创建缺失路径：

```bash
python scripts/task_observability_report.py \
  --cache-db data/cache/cache.db \
  --audit-db data/audit.db \
  --since 2026-08-27T03:25:08Z \
  --until 2026-09-26T03:25:08Z
```

输出为 JSON。`--since` 与 `--until` 必须带时区；任务按 `created_at` 纳入 UTC 左闭右开窗口 `[since, until)`。SQLite 默认写入的无时区时间按 UTC 解释。边界时刻等于 `since` 的任务包含，等于 `until` 的任务排除。

## 生产只读配方

在含本脚本的主 checkout 中运行。n305 的部署目录没有 git checkout，镜像也不复制 `scripts/`；命令通过标准输入把当前 checkout 的只读脚本送进容器 Python 运行，数据库仍由容器以只读模式打开：

```bash
ssh n305 'docker exec -i video-transcript-api python - --cache-db /app/data/cache/cache.db --audit-db /app/data/audit.db --since 2026-08-27T03:25:08Z --until 2026-09-26T03:25:08Z' < scripts/task_observability_report.py
```

如果生产 `storage.cache_dir` 改过默认值，将 `--cache-db` 改为该目录下的 `cache.db`。生产查询窗口应显式写成带 `Z` 的 UTC 时间。不要将运行中的 SQLite 文件复制到临时位置后把复制时间当作任务时间。

坏库、未知 schema、缺文件、无权限、无效时间都会返回非零退出码；不打印空报告掩盖错误。只读 CLI 接受 audit schema v5/v6，拒绝 v4 及其他未知版本，不会给数据库补列。

## 指标口径

- **任务计数**：audit 快照优先，cache 中仍存在的任务只补充缺失字段；按 `task_id` 去重。状态分别为成功、失败、进行中和未知，进行中包括 queued、processing、calibrating。
- **缓存命中**：按任务分别报告已观测到的完整命中、部分命中和命中状态未知数。只有正的 `cache_hit`/`cache_hit_partial` 计数能证明对应命中；没有计数器的旧快照或未走到相关分支的任务计为 unknown，不推断为 miss。
- **笔记/总结/章节状态**：输出已知状态分布与 `unknown_count`。缺字段、未尝试和旧快照均保持未知；零个任务与一个值为 0 的耗时不是同一件事。
- **端到端耗时**：每个任务用 `completed_at - created_at` 计算墙钟毫秒数。缺任一时间或完成早于创建的任务不进入样本，计入 `fields_missing.end_to_end_duration`。
- **阶段耗时**：从终态快照中的 PerfTracker 已完成区间汇总；每个任务每阶段的耗时是该阶段区间之和，`duration_ms.samples` 是有观测的任务数，p50/p95 使用 nearest-rank。成功/失败是 tracker 区间计数。未走到或没有阶段区间的任务不填 0。
- **LLM 用量**：`llm_usage` 的调用数、token 与 `duration_sum_ms` 独立列出。并行调用的耗时会相加，可能大于端到端墙钟；两者不能互换。`usage_missing_count` 表示有调用行但 provider 未回报用量；`tasks_without_usage_rows` 是没有调用行的任务数。
- **字段缺失**：`fields_missing` 按窗口任务显式统计未知状态、缺完成时间、缺观测快照、缺阶段样本与缺端到端样本。没有 `created_at` 的历史记录无法分配到窗口，单独输出为窗口外的 `unassigned_created_at_tasks`，不进入窗口任务数或字段缺失分母，也不使用 API 请求时间猜测。

## 边界与盲区

旧 audit 快照没有 `created_at`、详细笔记状态或 tracker 汇总时，cache 里若仍有同一任务行，可补起点与终态快照字段；任务清理后无法倒推。无起点的历史行不归入任何查询窗口。CLI 不读取共享媒体 `llm_status` 来补任务历史。阶段结果只反映 PerfTracker 已接线的区间，不拆解 LLM 协调器内部子步骤；LLM 用量也不包含未写入 `llm_usage` 的调用。
