# DESIGN-note：按写入/所有权/输出/进程边界拆四张预防型增量

## 目标

让章节诊断自洽、未移交接收资源释放不被错误记录短路、通知按方言编码用户 title、全链测试子进程不共用 `/tmp`。#227 已关闭（主脑 GET 回读 CLOSED/COMPLETED）；#211/#191/#199 留取证/blocked，不假装本批能闭环。

## 非目标

不改产品源码（本卡）；不并因标题相似的 PR；不新建状态框架/监控平台/启动框架；不加 retry/fallback/广泛 catch；不加时长上限；不重提双指纹/内嵌锚点/新章节端点；不跨仓改 registry；不声称消灭 502。

## 为什么不是分区 / 删除 / 约定

- **分区**：#226 多调用方已汇入 `save_llm_status`，再按调用方分区只会复制补丁。#204 接收槽/inflight/预算必须与同一请求的未移交文件同生共死，不能各写各的账本。#212 各 sink 方言不同，分区是「编码发生在该 sink」，不是新抽象。#216 已是独立 e2e 进程，缺的是私有 TMPDIR 而不是再开一层启动器。
- **删除**：#226 不能删 `chapters_error`（FAILED 诊断契约）。#204 不能删错误落盘，只能让释放不依赖它。#212 不能改库内 title。#216 不能靠 `TMPDIR=/tmp` 绕过。#199 不能删豁免假装已接 PWRS。
- **约定**：约定「skipped 时调用方传空串」会散落且与 None=不更新冲突。约定「finally 里别抛」挡不住 SQLite。约定「title 别用 markdown 字符」挡不住 X 帖。

消灭 > 处理 > 检测 > 报警：A/B/C/D 都在写入或测试边界消灭该类事故；#211 最多观测；#191 等数据；#199 外部。

## 方案要点与已否决方案

- **要点**：四张 **可独立测试/部署** 的实现增量（A–D）。#227 只关单。#211/#191/#199 见文末队列。
- **已否决**：给各 `llm_ops` 调用方补丁清 error；错误写库包一层 retry；跨平台万能反斜杠转义；统一 `TMPDIR=/tmp`；fork uvicorn protocol / 新 daemon；用时长上限代替 #191 校准；#227 双指纹/内嵌锚点/新端点。

### 增量 A — #226 status+error 在 `save_llm_status` 一致（局部根治）

- **改**：`src/video_transcript_api/cache/cache_manager.py`（`:2070` pop 条件从 `=="generated"` 放宽为「非 None 且非 failed」）。不必改 `llm_ops.py`。
- **测**：`tests/unit/test_cache_manager.py`；**待新增** skipped_short / skipped_no_timeline / disabled；现有 generated pop 与 untouched 保留必须仍绿。
- **非目标**：不改 API/页面；不引入新状态枚举。
- **根因**：None=不更新 与「本轮触碰且无 error」撞车，只有 generated 被强制 pop。
- **不变式**：[待新增锁 skipped_*] 新 status∈{skipped_short,skipped_no_timeline,disabled,pending,generated} ⇒ 无 `chapters_error`。[实测] failed 可带 error；[实测] status 未传 ⇒ 旧失败诊断保留（`test_untouched_layers_preserve_status_and_error`）。
- **入口验收**：生产 writer 写 llm_status.json（探针路径可升格为单测）。
- **负向红验**：把 pop 改回只认 generated，skipped_short 用例必须红。
- **预算**：总约 55–65 行（产品 15–25 + 测试 40）。耗时 unknown。无共享文件冲突（B/C/D 不碰 cache_manager 此函数）。未来实现需核对 `cache_manager.py` 及单测行数闸。
- **依赖**：无。单消费者理由：这是唯一权威合并点。

### 增量 B — #204 释放不依赖错误写库（局部根治）

- **改**：`src/video_transcript_api/api/routes/uploads.py` `:392-407`（行号已按 1596eca1 核实：finally 在 `:392`，`set_local_upload_error` 在 `:395`）。以资源所有权为契约：调整 finally 内顺序，先释放未移交资源（目录/预算/inflight/槽），随后再调 `set_local_upload_error` 写 error。写 error 失败**仍 fail-fast 上抛、不吞异常**，不加 retry/fallback/广泛 catch；本卡只承诺：该次写库失败不再使既有未移交资源释放被跳过（释放已先行完成）——不扩展为「所有 cleanup 路径永不失败」；相邻 cleanup 失败（如目录删除抛 OSError）只要求仍报错且请求接收槽释放，不声称目录删除失败后目录消失。主脑已核 `uploads.py:273` `if not receiver_acquired` 分支未持有槽（顾问“这处永久泄漏”错误，不写入设计）。`transferred=True` 分支不得 `clean_up_task`、不得误删已受理媒体。
- **测**：落 `tests/unit/test_upload_routes.py`（handler 级单测既有落点）。**待新增** 真实 handler+SQLite+raw stream 用例：注入 `set_local_upload_error` 分别覆盖抛 `sqlite3.OperationalError` 与 rowcount `ValueError` 两种情形，断言注入命中时 inflight/budget/接收槽/未移交目录计数归零（夹具蓝本：`/tmp/vta-261012-e0b600/probe-204-retry2.json`，sha256 `384a90ed582ee92ffe79e3caf525bb0e3e6b163f3aaefe352e33a5be2b51eedf`）；断言 `transferred=True` 时媒体文件不被误删；断言目录删除失败时仍报错且接收槽释放。现有 suite 无 `set_local_upload_error` 注入用例。
- **非目标**：不重试写库；不改受理成功路径；不加通用资源框架。
- **根因**：finally 把记账放在释放前面，记账一抛后续全跳过。
- **不变式**：[待新增] 注入命中且 HTTP 非静默成功时 inflight/budget/槽/未移交目录计数归零。[待新增] `transferred=True` 后媒体仍在。
- **入口验收**：`POST /api/uploads` octet-stream。
- **负向红验**：把 error 写回释放之前且让它抛，泄漏计数必须红。
- **预算**：总约 60–100 行（产品 20–40 + 测试 40–60）。耗时 unknown。高风险：所有权切换。不与 A 共享文件。未来实现需核对 `uploads.py` 及单测行数闸。
- **依赖**：无。

### 增量 C — #212 输出边界按方言编码 title（局部根治）

- **改**：覆盖三处：`channel.py` heading/status builder（`build_task_notification_heading` `:193-200`、`build_task_status_content` `:203+`，行号已按 1596eca1 核实）、`wechat.py` 长文（`notify_long_text` `:423,434`）、`router.py` view-link 与长文（`send_task_notification` `:210,214`、`send_view_link` `:245-255`）。sender 收到拼好的整段 markdown 无法只识别 title，改为各 channel 格式组装时、风控后将 title 按实际方言编码，然后拼成 content。企微 markdown_v2 与飞书 card 分方言处理；用户数据不在 producer 预编码、不二次编码；不改存储原始 title。可调用现有 builder 或最小两个消费者参数，但不锁死必须加接口。
- **测**：落 `tests/unit/test_wechat_notifier.py` 与 `tests/unit/test_feishu_channel.py`（既有 mock `send_markdown_v2`/`send_card` 配套测试同落点）。**待新增**：调用真实 builder 组装并传给 mock sender，断言 sender 实收 payload。**断言期望值不得从被测 builder 计算**（避免恒真逻辑），必须使用**独立规格夹具**断言真实 builder→sender 产物：特殊字符按方言转义、heading 结构不被换行破坏、原 title 不变且不双重编码。完整涉改 src 3 文件：`src/video_transcript_api/utils/notifications/{channel,wechat,router}.py`。
- **非目标**：不引入 Telegram；不改风控模块冒充转义。
- **根因**：title 当 markdown 字面量拼接。
- **不变式**：[待新增] producer payload 中特殊字符按方言处理后不再打乱 heading；原始 title 字段不变。
- **入口验收**：`notify_task_status` / 长文 `send_long_text`。
- **负向红验**：去掉编码后探针 payload 再出现裸 `*bold*` 必须红。
- **预算**：总约 60–100 行（产品 30–50 + 测试 30–50）。耗时 unknown。可能与通知文件重叠，勿与无关通知重构同车。未来实现需核对 `channel.py`、`wechat.py`、`router.py` 及单测行数闸。
- **依赖**：无。第二消费者：WeComChannel 与 FeishuChannel 共用 builder；编码落在各 channel 格式组装处，不锁定新抽象。

### 增量 D — #216 子进程继承短私有 TMPDIR（局部根治·仅测试）

- **改**：测试 scope 完整包含 `e2e/tests/local-upload-full-chain.spec.ts` 与 `tests/local_upload_real_server.py`（后者若用于消费侧实测必须允许改）。原 issue 仅 e2e 子进程临时隔离，宿主 pytest AF_UNIX 长路径属于 agent-config#3825，不扩大本卡。现状 `serverScratch = mkdtemp(join(tmpdir(), 'vta-upload-real-chain-'))`（`:135`）继承运行器父 TMPDIR，父目录长度不受控，不得当作短目录。改为显式构造短私有 scratch（如 `/tmp/vta-e2e-<一次性后缀>` 一级短根），完整路径按 AF_UNIX 108 字节预算校验（含未来任何 socket 文件名）；真实子进程环境注入该短私有根 `TMPDIR`，禁止再写死 `TMPDIR='/tmp'`；退出仍由 afterAll SIGTERM+rm 清理（`:141-165`）。生产 API 是 AF_INET TCP（`tests/local_upload_real_server.py:170-174`），无需改协议，Unix socket 仅作路径预算约束、不为它新造服务器。
- **fixture 断言 scope**（env/argv/cwd 取证的完整范围）：spec `:50-73`——python=`REPO_ROOT/.venv/bin/python`、argv=`['tests/local_upload_real_server.py','--run-dir',scratch]`、cwd=`REPO_ROOT`、env=spawnEnv 全键（PATH/HOME/TMPDIR/LANG/PYTHONUTF8/PYTHONUNBUFFERED）；server 侧 `--run-dir` 消费（`local_upload_real_server.py:157-200`）：cache/workspace/temp/audit.sqlite3/users.json 全部落 scratch，`VTAPI_CONFIG`/`VTAPI_USERS_JSON`/`VTA_UPLOADS_ENABLED`/`SENTRY_DSN=""` 均由该 run_dir 派生；ready 握手 marker `VTA_REAL_SERVER_READY`。
- **测**：同 spec；**待新增** 运行时断言子进程 env/argv/cwd（不靠源码阅读）：实测 `TMPDIR` 等于该次 scratch 且 ≠ `/tmp`，`--run-dir` 实参等于 scratch，cwd=REPO_ROOT。
- **非目标**：不改生产；不新增通用启动框架。
- **根因**：测试为绕过长路径把 TMPDIR 钉死到共享 `/tmp`。
- **不变式**：[待新增] 实测子进程 `TMPDIR` 等于该次 scratch 且 ≠ `/tmp`。
- **入口验收**：该 Playwright 全链 spec。
- **负向红验**：再写死 `TMPDIR: '/tmp'` 断言必须红。
- **预算**：总约 20–40 行（本卡全部为测试代码）。耗时 unknown。不与 A/B/C 共享文件。未来实现需核对 spec 及 server 测试 helper 行数闸。
- **依赖**：无。

## 关键不变式

1. [实测] generated ⇒ 无 chapters_error：`cache_manager.py:2070-2071`，`test_generated_after_failed_clears_stale_error`。
2. [实测] 未触碰章节 ⇒ 失败诊断保留：同文件，`test_untouched_layers_preserve_status_and_error`。
3. [实测·本卡探针] skipped_*/disabled 今日仍残留 error：待 A 锁死。
4. [实测·本卡探针] 写库失败会跳过释放：`uploads.py:392-407`，待 B 锁死。
5. [实测·本卡探针] title 未编码进 markdown：`channel.py:200`（heading 拼接）与 `:224`（content 拼接），待 C 锁死。
6. [读代码] e2e `TMPDIR='/tmp'`：`local-upload-full-chain.spec.ts:54`，待 D 锁死。
7. [实测·本卡窄验] #227 回归在 1596eca1 绿：`pytest tests/unit/test_notes_anchor_source.py -q` exit 0、7/7（主脑已 GET 回读 CLOSED/COMPLETED 关单；生产 MATCH 只作历史，不代替当前 pytest）。

未跑全量 suite。本卡非产品实现验收。

## 待验证前提

1. [推断] Sentry 默认 LoggingIntegration 能把 `uvicorn.error` ERROR 送进 GlitchTip——须 mock transport 或非生产 DSN，本卡 unknown。
2. [推断] #191 新部署后真实倍率分布未知；4.8 不得计入。
3. [外部阻塞] #199 PWRS token / runner ACL / 发布链。

## 验收路径

1. 入口：用户确认本 design 后由主脑派 A–D。**不声明无限并发**：四卡逻辑独立、文件不冲突，可在各自 worktree 并行开发并做端口/tmp 隔离，但**合并逐个进行**、每张等各自 CI 绿再合下一张；不碰 PR234。
2. 步骤：每张卡只改上列 src/test；窄测 + 入口测；禁止 docs-only 单独 PR 撞模型门禁（本设计随首张实现 PR 带上）。各卡窄测命令、全量 `make test` 耗时、Playwright 浏览器耗时本卡均未实测——实施卡按实测记录，未测前一律标 unknown，不得沿用本卡估算。未来实现要核对对应源文件与测试文件的行数闸。
3. 预期：A–D 各自谓词可本地证伪；#227 已关闭（主脑回读 CLOSED/COMPLETED）；#211/#191/#199 不在本批合入。

## 取证 / blocked 队列（不是 ready 卡）

- **#211 观测**：[实测·复跑] 真 TCP uvicorn 上注入坏响应头，发送阶段 stderr 出现 `ERROR:    Exception in ASGI application` + `h11._util.LocalProtocolError: Illegal header value b'text/plain\r\nX-Injected: 1'`（复跑命令、脚本 sha256 与证据路径见 triage.md #211；旧探针 JSON `log_record_count=0` 已撤销作为异常证据）。仍 unknown：GlitchTip 是否实际收事件。维持待取证，下一步 mock transport 或非生产 DSN 拿收据；不够再显式传 LoggingIntegration（仍 fail-open）。持续告警若做：重发 + fail-loud + selftest。不 fork protocol。
- **#191 取样**：历史 4.8 倍率出处仍未核（算式 4*1.2 同值不是测量出处），不得当完工样本。新部署日志一对锚点 `transcription_deadline duration=` 与结束 `耗时`；≥5 条且含 ≥1h recorder://；失败/截尾样本排除；max×1.2 向上取整 0.5。不 SSH 产日志（另开取样卡）。
- **#199 blocked**：仓内工作流未接 publish；豁免仍有效。不跨仓改 registry。
