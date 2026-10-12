# 八条 open issue 分诊（主干 c7dc7fcb）

基线：`c7dc7fcbb1727057ecf2c4d8c3f38e34d3136f72`（2026-10-12）。GitHub 只读快照 8 条 open。本卡未改产品源码、未跑 `make test`。证据四级：当前主干实测 / 读代码推断 / 历史记录 / 外部阻塞。标签只建议类型、领域、处理三轴，复用已有 `bug`/`enhancement`、`优先级:P2/P3`、`落点:本仓`。

## 分组（按可独立测试与部署边界）

1. 状态/资源生命周期：#226、#204
2. 输出边界：#212
3. 进程/观测边界：#216、#211、#199
4. 预算数据：#191
5. 已修待关：#227（不并入上列实现增量）

---

## #226 chapters_error 过期诊断

- **当前状态**：仍成立。权威写入点 `save_llm_status`（`cache_manager.py:1990-2103`）只对 `chapters_status=="generated"` pop error（`:2070-2071`）。`ChaptersStatus`：generated / skipped_short / skipped_no_timeline / failed / pending / disabled（`utils/llm_status.py`）。生产调用方：`llm_ops.py`（含 `:478` `:534` `:2499`）。`llm_ops.py:2416-2495` 在本轮触碰章节时把 `chapters_error=None` 原样传下去，合并语义不清旧值。
- **证据性质**：当前主干实测。仓外探针 `/tmp/vta-261012-e0b600/probe-226-212-204-211.json`：生产 `CacheManager.save_llm_status`，FAILED→skipped_short / skipped_no_timeline / disabled 磁盘仍留 `chapters_error`；只改 summary 的未触碰路径保留失败诊断；generated 仍 pop。证据固化（避免 /tmp 唯一副本）：复跑命令 `PYTHONPATH=<树>/src <venv-python> /tmp/vta-261012-e0b600/probe_triage.py`（使用项目 .venv 的 python 解释器）；脚本 sha256 `e17532cda059e5cf8bc45c5943734203e9cb2b417f26f78215413e51e2f29a54`、输出 JSON sha256 `dc37b18345077df5eb8c98c7f4364a9709df0d16f7b7bbfdb2bef95a99b68a0f`；关键输出：三转场 `stale_error_remains=true`、untouched 保留、generated pop。
- **领域分类**：缓存诚实状态 / 诊断字段一致性。
- **现有优先级**：初核快照为「无标签」；当前远端已有 `优先级:P2`+`落点:本仓`（主脑核对，本卡不查 GitHub）。建议补 `bug`：status+error 自相矛盾会误导排障。
- **标签建议**：`bug` + `优先级:P2` + `落点:本仓`。
- **处理谓词**：`save_llm_status` 在新 `chapters_status` 非 None 且非 failed 时去掉 `chapters_error`；未触碰（status=None）仍保留旧失败诊断。锁：现有 `test_generated_after_failed_clears_stale_error` / `test_untouched_layers_preserve_status_and_error`（`tests/unit/test_cache_manager.py:1810+`）；**待新增** skipped_*/disabled 转场。
- **独立增量归属**：实现增量 A（状态写入边界一处改）。
- **阻塞**：无。

## #204 上传错误标记写库失败跳过释放

- **当前状态**：仍成立。`uploads.py:392-407` finally 先 `set_local_upload_error`（`:395`），异常会打断后续 `clean_up_task` / `release_upload_temp` / inflight.release；`receiver_slots.release` 在同一 finally 后段同样到不了。`transferred=True` 才跳过未移交清理。
- **证据性质**：当前主干实测。真实 handler + SQLite + raw stream：`declared_size_mismatch` 注入 `set_local_upload_error -> sqlite3.OperationalError`。结果：注入命中 `error_code=declared_size_mismatch`，HTTP 500，inflight/budget 已获取未释放，接收槽未释放，临时目录残留 `.../upload-temp/task_task_*`。首次探针因 worker `__qualname__` 未就绪拿到 503，**不把 503 当缺陷**。历史 issue 正文为旧行号，当前行号已变（`:392-407` 已按 1596eca1 核实）。证据固化：命令同 #226（`probe_triage.py` probe_204 复跑）；输出 `/tmp/vta-261012-e0b600/probe-204-retry2.json` sha256 `384a90ed582ee92ffe79e3caf525bb0e3e6b163f3aaefe352e33a5be2b51eedf`；关键输出：`injection_reached=true`、`http_status=500`、inflight/budget held 未释放、`slot_released=[]`、`leaked_temp_dirs` 1 项、`resource_release_skipped=true`。
- **领域分类**：上传接收资源所有权。
- **现有优先级**：P2 合理（个人规模、故障已 HTTP 500；默认上传关闭不否定缺陷）。
- **标签建议**：保持 `优先级:P2` `落点:本仓`，补 `bug`。
- **处理谓词**：以资源所有权为契约：未移交资源的释放不依赖错误写库成功，覆盖 SQLite OperationalError 与 set_local_upload_error rowcount ValueError；写库错误仍 fail-fast 上抛不吞异常；相邻 cleanup 失败（如目录删除）仍报错且保证接收槽释放；`transferred=True` 后不得误删已受理媒体。主脑已核 `uploads.py:273` `if not receiver_acquired` 分支未持有槽（顾问“这处永久泄漏”错误，不写入设计）。**待新增** handler 注入测试落 `tests/unit/test_upload_routes.py`；现有 suite 无 `set_local_upload_error` 字符串。
- **独立增量归属**：实现增量 B。
- **阻塞**：无。不加 retry/fallback/广泛 catch。

## #212 通知 markdown title 未按方言编码

- **当前状态**：仍成立。`channel.py:build_task_notification_heading`（`:193-200`）只做风控，把 title 拼进 heading；`build_task_status_content`（`:203+`）同样。`wechat.py:notify_task_status` 走该 builder；`wechat.py:423,434` 与 `router.py:210,214` 另有 `## {safe_title}`。仓内无 Telegram sink。wecom-notifier 包几乎无 markdown escape。
- **证据性质**：当前主干实测。探针捕获 producer 字符串：title `X *bold* _shot_ [link] \`code\` #2500\nsecond-line` 原样出现在 heading 与 status payload，换行拆开 heading。证据固化：命令同 #226（`probe_triage.py` probe_212）；关键输出（同一 JSON sha256 `dc37b18345077df5eb8c98c7f4364a9709df0d16f7b7bbfdb2bef95a99b68a0f`）：`raw_title` 与 `heading_payload`/`status_content_payload` 逐字符一致，note=`risk-control is not markdown escape`。
- **领域分类**：通知输出边界。
- **现有优先级**：P3 合理（外观）。
- **标签建议**：保持 `优先级:P3` `落点:本仓`，补 `bug`。
- **处理谓词**：各 channel 格式组装时、风控后将 title 按实际方言编码后拼成 content（覆盖 channel.py heading/status、wechat.py 长文、router.py view-link 与长文三处）；库内原始 title 不变，不预编码、不双重编码。**待新增** 独立规格夹具断言真实 builder→sender 产物，断言期望不得从被测 builder 计算。落 `tests/unit/test_wechat_notifier.py` 与 `tests/unit/test_feishu_channel.py`。
- **独立增量归属**：实现增量 C。
- **阻塞**：无。

## #216 全链子进程 TMPDIR=/tmp

- **当前状态**：仍成立。`e2e/tests/local-upload-full-chain.spec.ts:51-70`：`spawnEnv.TMPDIR='/tmp'`，`cwd=REPO_ROOT`，argv=`[.venv/bin/python, tests/local_upload_real_server.py, --run-dir, serverScratch]`。`serverScratch` 来自 `mkdtemp(join(tmpdir(), 'vta-upload-real-chain-'))`（`:135`），afterAll SIGTERM + `rm` scratch（`:141-165`）。子服务用 `AF_INET` TCP（`tests/local_upload_real_server.py:170-174`），不是 Unix socket。宿主 pytest AF_UNIX 长路径属于 agent-config#3825，不扩大本卡。
- **证据性质**：当前主干读代码（本卡未跑 Playwright）。历史子进程采样 `TMPDIR=/tmp` 见 issue 正文提交 `b50c861`。
- **领域分类**：测试隔离 / 进程边界。
- **现有优先级**：P2 略高，可维持（审查留下的测试债，非生产阻断）。
- **标签建议**：初核快照建议补 `enhancement`；主脑实际已补 `bug`（两者分开记录：远端实际落库以 `bug` 为准，`enhancement` 不追溯改远端）。保持 `优先级:P2` `落点:本仓`。
- **处理谓词**：子进程 `TMPDIR` 为短私有目录（兼顾 AF_UNIX 108 字节上限，不强制改 TCP），子孙继承，退出清理；断言实际 env/argv/cwd。禁止再写死 `TMPDIR=/tmp`。测试修改 scope 包含 `e2e/tests/local-upload-full-chain.spec.ts` 与 `tests/local_upload_real_server.py`。
- **独立增量归属**：实现增量 D（仅 e2e，不改生产）。
- **阻塞**：无。

## #211 发送阶段异常观测

- **当前状态**：[实测·复跑 2026-10-12] 真 TCP uvicorn 上发送阶段注入坏响应头，stderr 出现白名单行 `ERROR:    Exception in ASGI application` 与 `h11._util.LocalProtocolError: Illegal header value b'text/plain\r\nX-Injected: 1'`（响应 0 字节，**0 字节本身不当异常证据**）。旧探针 `/tmp/vta-261012-e0b600/probe-211-uvicorn.json`（`log_record_count=0`/`local_protocol_seen=false`）**撤销**作为 h11 异常证据：其进程内 ListHandler 被 uvicorn dictConfig 清空，且当时 stderr 未落盘。`observability.py` 只 `zlx_ops_sdk.init`，不提 LoggingIntegration / uvicorn.error；SDK `sentry_sdk.init(**kwargs)` 不关默认集成；LoggingIntegration 默认 `event_level=ERROR`，忽略名单不含 `uvicorn.error`。本卡未设 `SENTRY_DSN`、未连生产 GlitchTip，**GlitchTip 是否实际收事件仍 unknown**。
- **证据性质**：当前主干实测（uvicorn stderr）+ 读代码推断（Sentry 默认集成）+ 外部阻塞（GlitchTip 收据）。复跑命令：`PYTHONPATH=<树>/src <venv-python> /tmp/vta-261012-closeout-211/probe_211_uvicorn_rerun.py 2>/tmp/vta-261012-closeout-211/probe-211-stderr-20261012T1012.log`（使用项目 .venv 的 python 解释器）；脚本 sha256 `352cf699044f3413320c9d197eb536f8d06dde2cd4cdb07e00fd24b950d5dabd`；stderr 证据 sha256 `46773ca201cca3e6813347ea6e9efc6c93a947121dff85ec68ba25cfb28a2e46`（25 行，白名单行在第 5 与第 25 行）；结果 JSON sha256 `73b302dd34acbbb1e16ec318d848d5eb2a1afa4b8a0e146f956956e6ebdc82dd`。旧脚本 `probe_211_uvicorn.py` sha256 `5567ac1fb3e9a1090b631c5096974c54ca1410b3915d8e2b73abde14070d63b0` 仅存档。
- **领域分类**：可观测性。
- **现有优先级**：P2 合理。这是观测缺口，不消灭 502。
- **标签建议**：保持 `优先级:P2` `落点:本仓`，补 `bug`。
- **处理谓词**：维持待取证。mock/transport 或一次非生产 DSN 证明 `uvicorn.error` ERROR 能进事件；若选持续告警必须重发 + fail-loud + selftest。未证明前不关单。
- **独立增量归属**：取证队列，不进 ready 实现卡。
- **阻塞**：生产 GlitchTip 收据本卡不可得。禁止 fork protocol / 新 daemon。

## #199 ui-evidence-publish

- **当前状态**：`.github/workflows/ui-browser.yml` 仅 `chromium-regression`（self-hosted `ci`，`npm run test:browser`），无 ui-evidence-publish / PWRS 步骤。`AGENTS.md:3` 豁免仍在。评论（2026-10-10）：解除条件是豁免三要素任一变化。REST 稍后读到额外标签 `enhancement`、`领域:运行与交付`（与首次 `gh issue list` 不完全一致，按后读为准）。
- **证据性质**：当前主干读代码 + 历史评论。`gh token` 不可见 ≠ 不存在。
- **领域分类**：CI / 证据发布。
- **现有优先级**：P2 作跟踪单可接受。
- **标签建议**：保持 `enhancement` `优先级:P2` `落点:本仓`；已有 `领域:运行与交付` 则复用，不新造领域标签。
- **处理谓词**：token、自建 runner 对该仓 PWRS 访问、真实发布链三者都验证后再接模板并去掉豁免。本仓改完不能独自闭环。
- **独立增量归属**：blocked 队列。不跨仓改 registry。
- **阻塞**：外部 token / runner ACL / 发布链。

## #191 DEADLINE_REALTIME_FACTOR 校准

- **当前状态**：`capswriter_client.py:128-129` 仍 `4.0` / `120.0`，预算 `:270`。Owner 评论（2026-10-06）：用生产数据校准，**不加时长上限**；≥5 条真实任务且含 ≥1h `recorder://`；max×1.2 向上取整 0.5；max≤4.0 则只关单不改代码。历史「4.8 倍」出处仍未核（算式 4*1.2 同值不是测量出处），来自被旧预算掐断的未跑完任务，**不得当完工样本**。
- **证据性质**：读代码推断（公式仍在）+ 历史记录（owner 决策）。本卡不 SSH、不读生产原始日志。
- **领域分类**：转录预算。
- **现有优先级**：P2 合理（等数据）。
- **标签建议**：保持 `优先级:P2` `落点:本仓`；类型 `enhancement`（校准）即可，不必升 P1。
- **处理谓词**：样本规则满足后再改常量+注释+预算等价测试；或证明新样本 max≤4.0 则关单。
- **独立增量归属**：取证/数据取样卡，不进本批 ready 实现。
- **阻塞**：合格生产样本。

## #227 notes 锚点同源（已关闭）

- **当前状态**：已关闭（主脑 GET 回读 CLOSED/COMPLETED）。代码已在主干。合并：PR #228 `19d5f23d`、#229 `dcc27ea6`、#231 `45c9f56d`（均祖先于本基线）。回归：`tests/unit/test_notes_anchor_source.py`（`TestCachedDialogsSeedAnchorsNotesReader` / `TestSegmentsAnchorIsNotRegressed` / `TestDeclaredKindFailsSafe`）。
- **证据性质**：当前主干读代码 + git log/show；生产 MATCH 为**历史记录**（`agent-worklog/20261011T005016Z-pi-lead-.md`：kind=`cached_dialogs` 518 段，reader 指纹 MATCH，notes generated）。二者不可混用。
- **领域分类**：章节/笔记锚点。
- **现有优先级**：初核快照为「无 P 标签」；关闭前远端已有 `优先级:P2`+`落点:本仓`，`bug` 已有。
- **标签建议**：保持 `bug` `落点:本仓`；分开「当前测试」与「历史生产」。
- **处理谓词**：`pytest tests/unit/test_notes_anchor_source.py -q` 在当前 HEAD 绿即可关。**已验并关闭**：1596eca1 上以项目 .venv 解释器+显式 `PYTHONPATH=<卡树>/src` 运行，exit 0、7/7 通过（完整输出 `/tmp/vta-261012-closeout-227/pytest-notes-anchor.out`，sha256 `4b7ddc37fcc056878a4b2253828a2d3a98c92bc9ff8c3366e506c97891569e85`；该窄验证不代表产品全量验收）。主脑回读确认已关单。已否决：双指纹兼容、产物内嵌锚点、新 `/regenerate_chapters` 端点。
- **独立增量归属**：已关单，不派实现卡。
- **阻塞**：无。

## 标签总表

| # | 类型 | 优先级 | 落点 | 处理 |
|---|---|---|---|---|
| 226 | bug | P2 | 本仓 | 可派增量 A |
| 204 | bug | P2 | 本仓 | 可派增量 B |
| 212 | bug | P3 | 本仓 | 可派增量 C |
| 216 | bug（建议 enhancement 分开记） | P2 | 本仓 | 可派增量 D |
| 211 | bug | P2 | 本仓 | 取证，不派实现 |
| 199 | enhancement | P2 | 本仓 | blocked |
| 191 | enhancement | P2 | 本仓 | 取样后校准 |
| 227 | bug | P2（初核快照无） | 本仓 | 已关闭（7/7 验绿） |

不新造几十个领域/处置标签。失败可见性：#211 GlitchTip 收据 unknown；#191 生产倍率 unknown；#199 外部链路 unknown。
