# F-r1 独立审查结论

failure-visibility: p2-only

- 固定审查头：`de3ff7b6671418ae7746649ef2167d5778c790ef`
- 基线：`0ae4bffe911fa93bb858d24fd173aee602af9c0b`
- F 软件增量：`21329ec6ef8be569b2c4f29d5a3cfbddaa7ad571..de3ff7b6671418ae7746649ef2167d5778c790ef`
- 风险档：personal；Task-Id 与 Fixes-Issue 在任务卡中为空。
- 软件工具验收：**未通过，一项 P2**。M6 工具的 ancestry 状态不满足“unknown 与已证不兼容分开”的要求。
- 交付阻断：**阻断 F/M6 软件工具验收**，直到下述状态误报修正或由 lead 明确裁决接受。此结论不等于生产风险 P1。
- 生产启用：**仍阻断且证据为 unknown**。CLI 对所有场景均输出 `UPLOAD_ENABLE_BLOCKED` 并返回 2；本轮没有访问生产服务、卷、unit、容量或恢复域。

## 范围与隔离

已枚举 `0ae4bffe..de3ff7b6` 的 18 个变更路径，检查了允许审查的工具、测试、操作文档、配置示例、compose 及规划差分。按任务卡禁止读取 `docs/sessions/261008-local-upload/progress/F-progress.md`；没有读取实现报告、旧 review、顾问报告、实现卡或 PR #208 正文。固定设计 `docs/sessions/261008-local-upload/design.md`、M6 与 GOALS/六份 goal 及 `roadmap-after-C.md` 用于核对原门槛。M4 仍阻塞、M5 未开始、M6 只允许软件准备；M4/M5、真实容量/恢复、环境证据和授权仍是完整验收与生产启用前置，没有被规划差分删减。

独立运行使用官方 scratch-worktree 入口，固定 `H0=de3ff7b6671418ae7746649ef2167d5778c790ef`。成功的完整验证树为 `<scratch>/worktree`，开始时状态空，`config/config.jsonc`、`data`、`.venv` 均不存在。示例配置 inode 分别为 scratch `2049:3674756`、主树 `2049:2883693`、实现树 `2049:18877700`，没有共享 inode。新建 `.venv` 使用本地 CPython 3.11.15，`UV_OFFLINE=1`；配置、用户、媒体、SQLite、日志和 pytest 临时文件都在该 scratch 临时域。结束时 git status 为空，官方包装器已移除 scratch；最终 subprocess payload 另在 `<scratch>/worktree` 捕获并自动清理。

所有运行检查置于 `bubblewrap --unshare-net` 网络命名空间：仅 `lo` 接口（`127.0.0.1/8`、`::1/128`），无外部路由；绑定写权限仅 scratch 域和本机 uv 包缓存。服务启动、URL fixture、ASR rejector 均通过 loopback。没有测试 systemd 或生产环境。

## Findings

### F-R1 — P2：Git ancestry 查询错误被误报为已确认不兼容

- 违反不变式：spec §1 要求 unknown 与明确不兼容保持两态；spec §5 要求真实源码 ancestry 证据与运行环境未知分开。
- 代码：`scripts/ops/local_upload_check.py:113-127`，`git merge-base --is-ancestor` 的任何非零退出都落到 `incompatible`。测试 `tests/unit/test_upload_ops.py:56-63` 只锁定真实祖先/非祖先，没有锁定 ancestry 查询本身失败时必须 unknown。
- 真实条件与复现：在 scratch 内建只含一个普通 commit 的临时 Git 仓库，令其 HEAD 为 `aa40829b58a76f62cf840856ba667b79085275cd`。`git -C <repo> merge-base --is-ancestor 6af391d20edab8dfd8b320ce6b91d4d19e3260e2 <HEAD>` 返回 128，stderr 为 `fatal: Not a valid commit name 6af391d20edab8dfd8b320ce6b91d4d19e3260e2`；同一输入调用 `compatibility_status` 却返回 `incompatible`。这是缺少最低安全 SHA 对象/不完整历史时的查询错误，不是“确认该源码早于最低版本”。
- 对照：实际 H0 仓库返回 `safe-lineage`；已存在的 pre-A SHA `38b299468daaf309608f46ffe1c59886b0922c04` 返回 `incompatible`；全零、无法解析的 source SHA 返回 `unknown`。所以错误限定在 commit 能解析、但 ancestry 查询自身失败的路径。
- P1 两问：①本轮在完整 H0 源仓实测到的 merge-base 查询成功；没有生产 checkout/部署证据证明不完整仓路径在真实服务环境触发，不能定 P1。②触发后会给出错误的保守状态，但 `run_check` 仍打印生产容量/恢复/ingress unknown、`UPLOAD_ENABLE_BLOCKED` 并返回 2；未发现它能启用上传或造成数据损害。定 P2。
- 阻断理由：不会绕过安全开关，但会把“无法证明”呈现为“已证不兼容”，违反本卡明确的软件工具状态契约，因此阻断 M6 软件工具验收；不据此宣称存在生产 P1。

没有其他达到可执行 finding 门槛的意见。`docker-compose.deploy.yml` 默认 `.env` 形态的来源报告正确；`send_terms` 示例删项经真实加载器与 consumer 核对，不改变本轮已接受配置默认行为。`deployment_env_source` 对非默认 development compose 的 `- path:` 形式没有做完整 YAML 解析；任务文档的运维命令明确指定 tracked deploy compose，故不作为本轮交付 finding。

## 七项不变式的证据结论

1. **解析器、限额、磁盘、时长、bytes、ancestry、开关。** `scripts/ops/local_upload_check.py:26-127,143-211` 消费 `load_and_validate_config`、真实 data 路径和 `ffprobe`；`tests/unit/test_upload_ops.py:11-63` 锁正有限限额、size/duration 与 ancestry 正反例。真实 CLI 对 4 MiB、0.02 小时、并发 2、temp budget 12 MiB 与 20 秒/1,920,044 字节合成样本报告 `within_declared_limits`；source 状态漏洞见 F-R1。`process_upload_gate=exact_true` 仍得到 `UPLOAD_ENABLE_BLOCKED` 与退出码 2。无真实生产证明仍为 unknown。
2. **受限 HTTP/SQLite/worker/ffprobe/URL 样本与 producer payload。** `scripts/perf/local_upload_capacity.py:96-135,138-207,238-426`；`tests/unit/test_upload_ops.py:66-111`、`tests/integration/test_upload_restore.py:150-180`。5 秒真实 API 探针：PCM s16le mono 48 kHz，480,044 字节；两份 raw HTTP receipt，持久 SHA-256 `27cb785df0cedf3e8136cf6893ae9a93a7220efc0420bd3e03dfc1c367f29638`；受控 URL 请求 1 次且和 upload 区间重叠；loopback ASR rejector 4 次连接，3 个 worker 均以预期失败终态结束。报告将 ASR/生产容量明确标 unknown。另一个 20 秒样本的 1,920,044 字节 SHA-256 为 `d85c8af3ff68fa7dec3d39d1b430293752de59f30da354b7ab30ea2be6c1c5f5`。
3. **snapshot/旧 DB 恢复/fresh consumer/保护。** `tests/integration/test_upload_restore.py:40-148,183-249` 使用实际 CacheManager SQLite、转录正文 bytes、撤销前 snapshot、撤销后 DB 和独立 fresh 进程；旧 snapshot 的 `revoked_at=None` 由外部 false gate 保持不可读，进程实际输出 `UPLOAD_DISABLED`；当前 cleaner 保留有效 never 正文与 root。guard-only A SHA 的独立消费者对同类真实 producer 结果读出正文，并拒绝 upload/blank legacy alias。既有 `tests/unit/test_local_upload_policy.py` 还覆盖 active 30d/never 与清理消费者。完整 production 恢复域、审计卷及撤销权威源未实测，仍 unknown。
4. **失败、清理、启动、进程退出。** 容量实验对运行时间强制 1–30 秒；真实探针启动 FastAPI、等待 worker 终态，并由上下文回收进程、HTTP fixture 与临时域；loopback ASR 失败作为预期显式记录，没有 fallback/retry/catch。未单独注入 systemd/生产启动失败或进程中断，不能据此宣称生产清理证明。
5. **旧源码与配置/操作文档。** `MINIMUM_SAFE_SHA` 只断言 Git ancestry，不声明 OCI digest 与源 SHA 绑定；文档同时保留这一限制。模板保留四项 `null`，真实 parser 可消费合成正有限配置。`send_terms` 删除自 example 的默认 consumer 为 false，且严格 loader 不接受该未知字段；不改变本轮有效配置路径。CLI 只输出白名单限额、disk free、sample 和状态，不读取 dotenv，不打印配置秘密。
6. **scratch/隔离。** 使用新 known-empty scratch、独立 `.venv`、固定 H0，loopback-only network namespace；未在主树、实现树或 review 树跑 pytest/make/服务。三个成功执行阶段结束均由 wrapper 清理，主树与实现树未写入。
7. **软件交付与生产启用分离。** 当前工具有真实本地行为，`run_check` 仍 fail closed；生产容量、ASR 实际工作集、ingress、OCI/source 绑定、systemd 身份/mount、完整恢复域、CI/systemd 环境验证与部署授权全部保留 unknown，不能由本地样本或文档替代。

## 命令与实际结果

完整 scratch 命令入口：

```text
<agent-config>/scripts/git/scratch-worktree.sh <main-repo> de3ff7b6671418ae7746649ef2167d5778c790ef -- /bin/bash -s
```

入口内先记录空树/inode manifest，再以 `bubblewrap --unshare-net` 运行；环境为 `UV_OFFLINE=1`、`UV_PYTHON=<local CPython 3.11.15 binary>`、`UV_PYTHON_DOWNLOADS=never`，测试 `.venv` 位于该 scratch。实际执行命令与结果：

```text
uv sync --frozen                                      exit 0
VTA_UPLOADS_ENABLED=true uv run --frozen python scripts/ops/local_upload_check.py   --config <scratch>/evidence/local-upload-config.json   --data-dir <scratch>/evidence/data   --compose docker/docker-compose.deploy.yml --repo .   --source-sha de3ff7b6671418ae7746649ef2167d5778c790ef   --sample <scratch>/evidence/sample.wav             exit 2 (expected blocked)
uv run --frozen pytest -q tests/unit/test_upload_ops.py   tests/integration/test_upload_restore.py            exit 0
make test                                             exit 0 (one full run)
```

真实 CLI 关键输出：

```text
upload_limits=valid
configured_limits=max_file_mib:4_max_media_hours:0.02_receive_concurrency:2_upload_temp_budget_mib:12
sample_bytes=1920044
sample_duration_seconds=20.000
sample_limit_check=within_declared_limits
process_upload_gate=exact_true
source_compatibility=safe-lineage
production_capacity_evidence=unknown
restore_domain_evidence=unknown
production_ingress_evidence=unknown
UPLOAD_ENABLE_BLOCKED: production capacity has not been measured; independent restore domain has not been proved; production ingress and runtime image have not been verified
```

真实容量工具实际 child payload（只记录安全白名单字段及 config hash，不输出 token/config 原文）：argv 为 scratch `.venv/bin/python3 <scratch>/main.py --start --config <scratch>/tmp/local-upload-capacity-vsz7iwpw/sandbox-config.json`；cwd 为 H0 scratch。env keys 为 `HOME,LANG,LC_ALL,PATH,PYTHONPATH,TZ,VTAPI_USERS_JSON,VTA_UPLOADS_ENABLED`，HOME、用户 JSON、配置和所有 storage/log 路径均在 scratch；`VTA_UPLOADS_ENABLED=true`。实际配置 5,585 bytes，SHA-256 `803696be2e74b266790175c8b4f655987848c4af22af52b44be9bc73276a3b00`；API `127.0.0.1`、ASR `ws://127.0.0.1:<fixture-port>`、allowlist `127.0.0.1/32`。真实样本结果：`raw_http_receipts=2_of_2`、`controlled_url_upload_overlap=confirmed`、`worker_terminal_states=failed,failed,failed`、`asr_capacity=unknown_external_model_not_called`、`production_capacity=unknown_production_ingress_and_disk_quota_not_measured`。

最小 mutation 在 scratch 将实际 `sample["bytes"] > limit` 改成 `<`，执行 `uv run --frozen pytest -q tests/unit/test_upload_ops.py::test_sample_limit_check_compares_real_file_size_and_duration` 得退出码 1，失败为 `AssertionError: assert 'exceeds_file_limit' == 'within_declared_limits'`；`ImportError/ModuleNotFoundError=False`。源码恢复后 scratch git status 为空。

隔离 manifest（full verification scratch）：`scratch_sha=de3ff7b6671418ae7746649ef2167d5778c790ef`、`status_before=`、`knownempty_config/config.jsonc=absent`、`knownempty_data=absent`、`knownempty_.venv=absent`、scratch/main/implementation example inode=`2049:3674756 / 2049:2883693 / 2049:18877700`、`link_count=1`、`links=lo`、`external_routes=none`、`network_guard_loopback_only=True`。结束时 `scratch_status=`；`config/config.jsonc` 不存在。测试在 scratch 内创建的数据与 venv 随 wrapper 清理。

## CI、OCR 与未证事项

- 只读查询 `gh pr view 208 --json headRefOid,state,statusCheckRollup`：head 等于 H0、state OPEN；`gate / quality` 为 SUCCESS，但 `gate / primary` 与 `gate / ocr` 为 SKIPPED；`gate (draft)` 聚合 SUCCESS 不代表主审通过。没有打开 PR 正文。当前 PR rollup 不能替代本地/生产环境证据。
- 派发时主干基线查询为 `gh api request failed`；继承红结论未能判定，没有据此归因本轮。
- 按卡面 OCR 在主脑前置扫描已尝试，外层 55 秒超时且 stdout envelope 零字节；本轮没有重启工具，OCR 结论仍 unknown，不是 clean 或 skipped。
- 未运行 systemd/真实服务/真实卷/备份恢复、生产磁盘配额、真实 ASR/LLM、真实 ingress、裸 shell 的部署证据或用户授权流程。以上均不是本地代码 P1 证据，全部保持 unknown。
