failure-visibility: p2-only

# F-R2 独立审查结论

审查固定在 main `0ae4bffe911fa93bb858d24fd173aee602af9c0b`、Fbase `21329ec6ef8be569b2c4f29d5a3cfbddaa7ad571`、增量 H0 `de3ff7b6671418ae7746649ef2167d5778c790ef` 与 H1 `13e78042ac4ff97ae0fd42fa15031848259f985d`。审查对象是 main..H1 与 Fbase..H1；先专项核验 H0..H1。

**结论：P2-only；软件交付暂阻断。** 两个需处理的问题是恢复测试没有对同一恢复快照和 token 做开关开启的可读正对照；容量脚本的一次直接运行在固定等待后连接被拒，成功与失败结果并存，无法确认一次运行是否能稳定产出样本。没有确认 P1。生产启用仍因生产容量、独立恢复域、ingress/runtime image 和部署授权未知而阻断，这属于未证生产条件，不是代码 P1。

## H0..H1 四问

| 问题 | 结论 |
|---|---|
| 是否只修获准的 Git 查询错误分类并补真实 Git 测试？ | 可审的软件改动仅将 `merge-base --is-ancestor` 的退出码 1 解释为不兼容、其他非零解释为 unknown，并新增真实 Git 缺失提交测试。增量还改动了禁止阅读的 F-progress 文件；该文件内容不纳入审查，因此对整个三文件增量不能作完整肯定结论。一次批量差分输出意外带出了该文件部分内容，属流程偏差，详见下文。 |
| 是否新增未经批准的抽象？ | 否；已审代码没有新增 helper、状态层或配置层。 |
| 是否新增无据状态/事实源/fallback？ | 否；沿用原有 `unknown`，没有第二事实源、fallback、重试或防御性 catch。 |
| 是否留下新旧双路径？ | 未见；兼容判定仍走同一 Git ancestry 路径。 |

H0 反向对照：在 H0 新 scratch 中只覆盖 H1 的 `tests/unit/test_upload_ops.py`，执行 `test_missing_minimum_commit_query_is_unknown_from_real_git`。pytest 退出 1，失败为业务断言 `assert 'incompatible' == 'unknown'`；不是导入、路径或夹具错误，说明新测试能区分旧实现。

## 发现

### P2：恢复拒读用例缺少同一快照的可读正对照

违反中性规格第 4、5 条的配对证据要求。`tests/integration/test_upload_restore.py:101-139` 生成真实上传和正文、复制撤销前的 SQLite/正文快照、恢复该快照，并让新进程以 `VTA_UPLOADS_ENABLED=false` 查询 token；断言拒读和 `UPLOAD_DISABLED`。但该测试没有再以 `true` 对同一个 `restored_cache` 与同一个 token 查询成功正文。`test_actual_guard_only_source_consumes_fixture_and_preserves_active_long_artifacts` 在 `:183-249` 的开启读取使用另一份新建缓存和真实 A 源码，不能证明刚恢复的那份快照及 token 本身有效。因此拒读断言仍可能在快照/token 映射错误时通过，无法排除“原本不可读”的恒真拒绝。

严重度为 P2：当前代码的 off gate 行为有直接断言，且另一个测试证明真实 A 消费者可以读真实 producer 产物；没有发现生产数据被错误公开的运行证据。交付仍阻断，因为任务规格明确要求恢复消费路径具有匹配的可读/不可读对照，而当前测试没有锁住这一不变式。P1 两问：真实使用中触发的是测试缺口而非已证实的错误读取路径，第一问不成立；没有已证实的不可接受生产后果，第二问不成立。

### P2：容量脚本固定等待后有一次真实连接拒绝

违反中性规格第 2、3 条对可执行容量样本和可解释结果的要求。`scripts/perf/local_upload_capacity.py:300-324` 启动 API 后固定 `sleep(2)`，仅检查进程仍存活就向 capabilities 端点发请求。H1 新 scratch 中直接执行文档命令 `python scripts/perf/local_upload_capacity.py --duration-seconds 20` 有一次退出 1，异常为 `ConnectionRefusedError`；进程当时仍存活。`tests/integration/test_upload_restore.py:150-180` 的单独 pytest 用例及一次 `make test` 均退出 0；strace 运行也在同一请求处失败，但 tracing 会影响时序。现有证据只能确认一次直接运行失败，不能确认根因或失败频率，也不能把它归咎于唯一的启动时序因素。

严重度为 P2：失败会中止本次本地样本并显式报错，不会启用上传、改数据卷或产生生产影响。交付阻断是因为所要求的直接容量运行未稳定给出样本，且成功/失败不一致尚未解释；测试套件退出 0 不能代替一次直接运行的有效测量。P1 两问：该次真实运行触发了，但发生在隔离 scratch 而非生产；没有不可接受后果，P1 不成立。

## 七项不变式核对

| 项 | 代码与测试锚点 | 结论 |
|---|---|---|
| 1. 配置、额度、磁盘/样本、源码兼容、unknown 与 blocked | `scripts/ops/local_upload_check.py:31-131,147-209`；`tests/unit/test_upload_ops.py:12-110,161-226` | 真实配置加载入口、正有限额度、ffprobe、实际磁盘剩余量及真实 Git ancestry 均有代码/测试。未提供生产证据时始终输出 `UPLOAD_ENABLE_BLOCKED` 并返回 2；无自动启用/部署/恢复。 |
| 2. 有界真实 HTTP、SQLite 回执和 loopback 对端 | `scripts/perf/local_upload_capacity.py:76-207,238-439`；`tests/integration/test_upload_restore.py:150-180` | 20 秒 PCM WAV、上传体 hash、真实 HTTP receiver、SQLite 回执、worker/ffprobe、受控 URL overlap、loopback ASR rejector 和临时目录采样都有实现及断言。直接样本运行结果有 P2 不确定性。 |
| 3. 观测阶段与跨进程 payload | `scripts/perf/local_upload_capacity.py:288-324`；`tests/unit/test_upload_ops.py:111-155` | 实际服务 argv/env 及实际写出配置字节已在被测进程运行时取证；网络命名空间无路由，实际连接目标仅 loopback。输出不宣称真实 ASR 容量或临时预算上界。 |
| 4. 快照、撤销、关闭 gate、长期正文保护 | `tests/integration/test_upload_restore.py:101-147`；实际 Resolver/cleaner | 实际 SQLite/正文复制、撤销后恢复旧 snapshot、fresh 关闭 gate 子进程拒读、never 行和正文由当前 cleaner 保留均有断言。缺同一 snapshot/token 的开启读取正对照，见 P2。 |
| 5. 最低安全旧源码与消费者 | `tests/integration/test_upload_restore.py:183-249`；`A_GUARD_ONLY_SHA=6af391d20edab8dfd8b320ce6b91d4d19e3260e2` | 用真实 A 源码 archive 消费当前 CacheManager 生成的 SQLite/文件，开启时读取正文，legacy/blank alias 拒绝，两个 cleaner 保留长期成果。仅证明源码行为，不证明 OCI image digest/生产镜像绑定。 |
| 6. 新 scratch 与隔离 | H1/H0 均使用精确 SHA 创建的官方 scratch worktree | 初始 `config/config.jsonc`、`data`、`.venv`、`.pytest_cache` 均不存在；无外部软链；与主仓及既有 F worktree 的 tracked inode 交集为零。运行使用新 venv、临时 HOME、scratch 外侧的运行临时域和无路由网络命名空间。 |
| 7. 软件证明与生产证明分开 | `docs/operations/local-upload.md:1-53`、compose 与示例配置 | 文档明确上传默认关闭、四额度为 null、真实容量/恢复/入口和部署授权未知；操作步骤不自动恢复、不触碰生产卷。生产证明仍未执行。 |

## 实测和未知

- H1 实际 CLI 用 `config/config.example.jsonc`、合成 1 秒 WAV、临时数据目录和 H1 source SHA 运行，退出 2；实际输出中样本为 96044 bytes / 1.000 秒，limits 为 unknown，source 为 safe-lineage，生产 capacity/restore/ingress 均为 unknown，并打印 `UPLOAD_ENABLE_BLOCKED`。这是预期的拒绝启用结果，不是生产证明。
- H1 容量子进程实际 argv 为 Python 的 `main.py --start --config <临时运行域>/sandbox-config.json`；观察到的环境含 scratch HOME、`PYTHONPATH=<scratch>/src`、合成用户 JSON 与 `VTA_UPLOADS_ENABLED=true`。producer 实际生成的配置为 5640 bytes，SHA-256 `aeb4c404fc31496f77961bd4a122a2be8986b5aa48b9425b95fed345643a7a00`；API host 为 127.0.0.1，ASR URL 为 loopback，allowlist 为 127.0.0.1/32，缓存、workspace、temp、audit 与日志路径均位于临时运行域，token 与本次 synthetic fixture 相符。
- H1 聚焦容量 pytest 用例退出 0；整次 `make test` 退出 0。报告到的 skip 共 5 项：3 项缺 CapsWriter preflight fixtures，2 项因执行环境 root 权限绕过 chmod 权限测试。另一次带 tracing 的定向运行退出 1，失败点为 capacity 请求连接拒绝。直接 CLI 的失败与上述通过结果不一致，因此不把 capacity 样本判为稳定通过，也不据此断言唯一根因。
- 未运行生产服务、systemd、真实 ingress、生产磁盘/ASR/代理容量、完整恢复域或 CI；GitHub 基线查询在派发时不可用，继承红无法判断。主脑 OCR 前置扫描只得到超时且空 envelope，没有 reviewed/skipped 结论；本卡未重跑，也不把它当成 clean。
- 卡面列出的 `GOALS.md` 与 `goals/local-upload/M6-recovery.md` 在固定 H1 工作树中不存在；评价锚点使用派发 spec、存在的 `docs/sessions/261008-local-upload/design.md` 和实际运维文档。没有读取实现报告、旧 review 或 PR 正文。

## 流程偏差

一次批量 `git diff` 意外把禁止读取的 `docs/sessions/261008-local-upload/progress/F-progress.md` 部分内容写入终端输出。该内容未作为证据或判断依据；但输入隔离已被破坏，不能把本报告称作完全无污染的独立审查。因此执行报告标记为 failed。对增量四问中可审的代码/测试结论不受该文件内容支持；该进度文件本身没有被审查。
