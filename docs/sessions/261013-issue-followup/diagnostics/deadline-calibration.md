# #191 现有生产数据校准

## 口径

- 来源为容器 `56664410447f349475c74af54b6def09e3bd732ae3df63ce2cb95ffacf3449c9` 的 `/app/data/cache/cache.db`；只读 URI、`query_only=ON`、单事务 `BEGIN/SELECT/ROLLBACK`。查询前 WAL/SHM 均已存在。
- 窗口：`created_at` 从 `2026-10-10T17:39:05Z`（部署后）至 `2026-10-13T06:27:13.835495Z`，完整取数，无最新条数截断。
- 纳入：非 speaker（`use_speaker_recognition=0`）、外层与快照均成功、`completed_at` 非空、transcription stage `count=1/successes=1/failures=0`、媒体时长与耗时为正有限数、无正缓存计数。tracker 未输出 `counters` 表示没有这些计数；失败、缓存、缺值、非数值、多 stage 均不纳入。
- 比率为 `transcription.elapsed_ms / 1000 / media_duration_s`。stage 耗时含转录包装、等待/重试及产物处理，是单次尝试耗时的上界。

## 生产聚合

- 40 条窗口记录均为外层与快照成功；25 条为非 speaker，16 条符合纳入条件，含 3 条媒体时长至少 3600 秒的 recorder；失败数为 0。
- 正确全量 `MAX=0.0913451203`（秒/秒），`MAX<=4` 为真。样本门槛满足；现有系数覆盖本窗口样本的耗时上界。
- 本次允许的新增只读事务/SSH 均成功，退出码 0；stdout SHA-256：`77aac85d65f9e8886596ba869f0b1bc83032b2bb951fce9a9c8179cf0a874c82`。私有逐条脱敏 JSON、argv、脚本、fixture 输出及命令哈希保存在本 dispatch evidence 目录。

## #191 判定

按 owner 门槛，建议保持当前系数并关闭 #191。此结论仅覆盖上述成功样本窗口，不保证未来永不超时；窗口内没有失败任务，也未从允许读取的安全状态字段发现具体 Caps deadline 反证。

原独审 verdict 仍保留 `UNKNOWN` 与 `FAIL（P2）` 历史：它记录的是当时 tuple/dict 汇总错误、未产出正确 MAX；本统计补充真实聚合，不改写旧 verdict，也不宣称旧任务已通过。
