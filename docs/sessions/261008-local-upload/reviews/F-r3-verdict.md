# F-r3 verdict

failure-visibility: skipped

## H1..H2 修复增量结论

固定范围 13e78042ac4ff97ae0fd42fa15031848259f985d..07bbd72192e7cce71f0498145d4fefb2c86a533d 只有 tests/integration/test_upload_restore.py 增加 15 行。新增内容在同一 pytest 临时恢复副本上补了 gate=true 正对照，以及 upload_ 无效令牌反例。未改产品/helper 源码，也没有增加抽象、状态源、fallback 或产品双路径；true 只传给本地子进程，不是生产重开入口。任务卡没有列出登记 finding ID，故无法独立核对外部登记表；改动范围与卡面声明的配对恢复测试一致。

四问：①本轮只在指定恢复测试函数内补锁；②无新抽象；③snapshot 的 SQLite 和正文 bytes 来自同一真实 producer，子进程环境显式覆盖开关，没有新增状态或降级路径；④false/true 是同一合成副本上的测试对照，不是两套产品实现。

## 固定 F 范围与软件不变式

完整范围为 21329ec6ef8be569b2c4f29d5a3cfbddaa7ad571..07bbd72192e7cce71f0498145d4fefb2c86a533d，排除 docs/sessions/261008-local-upload/progress/** 后涉及 9 个文件。H2 的设计与 M6 文件说明软件工具不构成生产授权；M6 仍为“进行中”，M4 阻塞、M5 未开始。

| 不变式 | 源码与锁定测试 | 本次证据/边界 |
|---|---|---|
| 1. 运维 CLI 使用真实 config loader、四项正有限额度/正整数并发、Git ancestry 三态与进程环境；不读 dotenv、不启用或恢复 | scripts/ops/local_upload_check.py:31-48,98-131,147-209；tests/unit/test_upload_ops.py:12-109,161-226 | 实际 CLI 使用 config/config.example.jsonc、scratch data、精确 H2 与 env -i 启动，输出 safe-lineage/unknown 生产证据及 UPLOAD_ENABLE_BLOCKED，exit 2。真实部署 .env 与 systemd 消费环境未测。 |
| 2. 容量样本由实际 HTTP/API producer 写入 SQLite，并核对 payload hash；仅限 loopback/合成介质，不代表生产容量 | scripts/perf/local_upload_capacity.py:96-135,138-207,238-427；tests/unit/test_upload_ops.py:137-159；tests/integration/test_upload_restore.py:165-195 | 目标测试通过；断言覆盖 2/2 raw HTTP receipts、实际接收字节 SHA-256、URL/上传重叠、ffprobe、worker 终态与 blocked 输出。测试捕获了脚本 stdout，具体 digest 未留在本次终端记录。ASR/LLM、生产磁盘配额、代理和容量上界均未知。 |
| 3. 恢复旧 SQLite/body snapshot 后，fresh resolver 的外部 gate=false 拒读；true 正对照只用于合成旧副本；cleaner 保留有效 never 与 root | tests/integration/test_upload_restore.py:101-162；配对增量同文件 141-154 | 同一实际 producer 生成 DB/body 后复制 snapshot；撤销源记录后只把该 snapshot 复制到临时 restore 目录，逐字节比较 DB/body；fresh 子进程 false 拒读且有 UPLOAD_DISABLED，true 读出预期正文，无效 upload_ token 不读且无 disabled 告警；真实 cache/task cleaner 保留正文/root。该旧 snapshot 本来就是 revoked_at=NULL，不能证明可信撤销来源或全恢复域。 |
| 4. A guard-only 或更新源码消费当前 producer 数据，合法分享可读，旧 token 别名与 blank 拒绝 | tests/integration/test_upload_restore.py:21,198-264；最低 SHA 6af391d20edab8dfd8b320ce6b91d4d19e3260e2 | H2 ancestry 查询为 true；目标测试通过并以 git archive 的 A 源码在独立解释器读取当前 producer 的 SQLite/body，断言合法正文可读、upload/blank 别名为空、cleaner 保留成果。ancestry 不证明 image digest 与源码绑定。 |
| 5. 本轮失败可见，不新增 retry/fallback/catch；真实 CLI 有终态 | scripts/ops/local_upload_check.py:27-28,193-209；scripts/perf/local_upload_capacity.py:210-214,257-310；上述 unit/integration tests | 运维 CLI 真实退出码 2，fail-closed；容量集成测试实际启动 API 并经 HTTP/SQLite 完成样本。其测试 teardown 等待进程退出，但本次没有独立记录 API shutdown exit code；不据单次启动声称稳定或 cleanup 已获生产证明。 |
| 6. 软件准备与上线分开 | docs/operations/local-upload.md:1-53；GOALS.md:7-12,24-26；goals/local-upload/M6-recovery.md:1-27；docs/sessions/261008-local-upload/design.md | 示例额度仍为空；诊断工具保持 blocked。未改生产配置，也未运行 CI、systemd、生产 ingress/image、真实恢复或部署授权流程。M4/M5 与完整 M6 验收不能勾完成。 |

## 实测与未证

两次验证均通过官方 scripts/git/scratch-worktree.sh，以 VideoTranscriptAPI 主仓和固定 H2 创建临时树，并在 env -i、unshare --user --map-root-user --net 中运行。每棵树起始 HEAD 精确为 H2、干净；data、config/config.jsonc、liveconfig、.venv、.pytest_cache 均不存在；检查到的 symlink 均在树内；pyproject.toml inode 与 main 旧树不同。ip link set lo up 后网络接口仅 lo，IPv4/IPv6 route 均空。uv sync --offline --frozen 成功，CPython 3.11.15，依赖从本地缓存安装。

- 原样 uv sync --offline --frozen && .venv/bin/python -m pytest -q tests/unit/test_upload_ops.py tests/integration/test_upload_restore.py：12 passed。
- 对配对用例的方向性红验：先断言 helper 环境赋值源码唯一命中，再只在 scratch 中把 VTA_UPLOADS_ENABLED 固定注入为 false；目标用例在 assert "RESULT=success" in enabled.stdout 处失败，exit 1。该 scratch 已自动移除。
- 实际 local_upload_check.py 调用 exit 2，理由为四额度无效/未提供样本、生产容量/恢复域/ingress 未证；无配置正文或凭据输出。
- 全量 make test：exit 0，pytest 进度到 100%；安静输出中可见 5 个 s 标记，但没有保留 skip 用例名/原因，也未保留全量分母，因此具体 skip 归因与数量标为 unknown，不重跑套件。输出含既有弃用警告。
- 未运行正式 CI 或 OCR；卡面说明此前 OCR 超时，本轮遵守要求未再调用。派发时 CI 基线不可用，继承红/新红对照未能判定。

## 处置与审查状态

未观察到可据当前软件不变式成立的代码 finding；这不构成生产容量、恢复域、撤销来源、image/source 绑定、模型质量或稳定性证明。软件实现未发现已证代码缺陷；但本卡要求的独立审查因下述禁读违规不能清为通过，因此独立审查交付门仍未满足，需由干净输入的新审查确认。

程序偏差披露：一次 rg -n 'send_terms' src tests config docs 的搜索误包含禁读目录，输出带出 docs/sessions/261008-local-upload/progress/F-progress.md:14；我看到了该一行，随后停止读取任何 progress 文件。该内容未作为此 verdict 的证据，但这次输入污染无法撤销，故整体 failure-visibility 标为 skipped，不声称独立 review pass。
