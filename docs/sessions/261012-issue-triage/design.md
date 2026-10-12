# DESIGN-note：按写入/所有权/输出/进程边界拆四张预防型增量

## 目标

让章节诊断自洽、上传失败必释放未移交资源、通知按方言编码用户 title、全链测试子进程不共用 `/tmp`。#227 关单；#211/#191/#199 留取证/blocked，不假装本批能闭环。

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
- **预算**：约 15–25 行产品 + 40 行测试。无共享文件冲突（B/C/D 不碰 cache_manager 此函数）。
- **依赖**：无。单消费者理由：这是唯一权威合并点。

### 增量 B — #204 释放不依赖错误写库（局部根治）

- **改**：`src/video_transcript_api/api/routes/uploads.py` `:392-407`。先释放未移交资源（目录/预算/inflight/槽），再尽力写 error；写库失败不得打断释放。`transferred=True` 分支不得 `clean_up_task`。
- **测**：**待新增** 真实 handler+SQLite+raw stream，只注入 `set_local_upload_error` 抛 `OperationalError`（本卡 `/tmp/vta-261012-e0b600/probe-204-retry2.json` 可作夹具蓝本）。现有 suite 无此字符串。
- **非目标**：不重试写库；不改受理成功路径；不加通用资源框架。
- **根因**：finally 把记账放在释放前面，记账一抛后续全跳过。
- **不变式**：[待新增] 注入命中且 HTTP 非静默成功时 inflight/budget/槽/未移交目录计数归零。[待新增] `transferred=True` 后媒体仍在。
- **入口验收**：`POST /api/uploads` octet-stream。
- **负向红验**：把 error 写回释放之前且让它抛，泄漏计数必须红。
- **预算**：约 20–40 行。高风险：所有权切换。不与 A 共享文件。
- **依赖**：无。

### 增量 C — #212 输出边界按方言编码 title（局部根治）

- **改**：`channel.py` 的 heading/status builder；核对 `wechat.py:416+`、`router.py:205+` 是否仍直接 `## {title}`。用户数据只在 sink 编码，不改存储 title。企微 markdown_v2 与飞书 card **分方言**，不要一套反斜杠。
- **测**：**待新增** 断言 mock `send_markdown`/`send_card` 的实发 payload（`*` `_` `` ` `` `#` 换行 链接）。
- **非目标**：不引入 Telegram；不改风控模块冒充转义。
- **根因**：title 当 markdown 字面量拼接。
- **不变式**：[待新增] producer payload 中特殊字符按方言处理后不再打乱 heading；原始 title 字段不变。
- **入口验收**：`notify_task_status` / 长文 `send_long_text`。
- **负向红验**：去掉编码后探针 payload 再出现裸 `*bold*` 必须红。
- **预算**：约 30–50 行。可能与通知文件重叠，勿与无关通知重构同车。
- **依赖**：无。第二消费者：WeComChannel 与 FeishuChannel 共用 builder，值得在 builder 做一处编码再按 sink 微调。

### 增量 D — #216 子进程继承短私有 TMPDIR（局部根治·仅测试）

- **改**：仅 `e2e/tests/local-upload-full-chain.spec.ts`（及如有必要的 `tests/local_upload_real_server.py` 只读断言点）。`spawnEnv.TMPDIR` 改为短于 AF_UNIX 上限的 scratch（例如 scratch 本身，需保证路径短），不要 `/tmp`。断言 env/argv/cwd；afterAll 已有 SIGTERM+rm，保持。当前 API 是 TCP，不为 Unix socket 新造服务器。
- **测**：同 spec；**待新增** 对子进程 env 的断言（不要只靠源码阅读）。
- **非目标**：不改生产；不新增通用启动框架。
- **根因**：测试为绕过长路径把 TMPDIR 钉死到共享 `/tmp`。
- **不变式**：[待新增] 实测子进程 `TMPDIR` 等于该次 scratch 且 ≠ `/tmp`。
- **入口验收**：该 Playwright 全链 spec。
- **负向红验**：再写死 `TMPDIR: '/tmp'` 断言必须红。
- **预算**：约 10–20 行。不与 A/B/C 共享产品文件。
- **依赖**：无。

## 关键不变式

1. [实测] generated ⇒ 无 chapters_error：`cache_manager.py:2070-2071`，`test_generated_after_failed_clears_stale_error`。
2. [实测] 未触碰章节 ⇒ 失败诊断保留：同文件，`test_untouched_layers_preserve_status_and_error`。
3. [实测·本卡探针] skipped_*/disabled 今日仍残留 error：待 A 锁死。
4. [实测·本卡探针] 写库失败会跳过释放：`uploads.py:392-407`，待 B 锁死。
5. [实测·本卡探针] title 未编码进 markdown：`channel.py:204,227`，待 C 锁死。
6. [读代码] e2e `TMPDIR='/tmp'`：`local-upload-full-chain.spec.ts:54`，待 D 锁死。
7. [读代码+历史] #227 同源：`test_notes_anchor_source.py`；生产 MATCH 只作历史，不代替当前 pytest。

未跑全量 suite。本卡非产品实现验收。

## 待验证前提

1. [推断] Sentry 默认 LoggingIntegration 能把 `uvicorn.error` ERROR 送进 GlitchTip——须 mock transport 或非生产 DSN，本卡 unknown。
2. [推断] #191 新部署后真实倍率分布未知；4.8 不得计入。
3. [外部阻塞] #199 PWRS token / runner ACL / 发布链。

## 验收路径

1. 入口：用户确认本 design 后由主脑派 A–D（可并行，文件不冲突）。
2. 步骤：每张卡只改上列 src/test；窄测 + 入口测；禁止 docs-only 单独 PR 撞模型门禁（本设计随首张实现 PR 带上）。
3. 预期：A–D 各自谓词可本地证伪；#227 用 `pytest tests/unit/test_notes_anchor_source.py` 关单；#211/#191/#199 不在本批合入。

## 取证 / blocked 队列（不是 ready 卡）

- **#211 观测**：先证明现有 `zlx_ops_sdk.init`→`sentry_sdk.init` 默认集成是否已收 uvicorn ERROR；不够再显式传 LoggingIntegration（仍 fail-open）。持续告警若做：重发 + fail-loud + selftest。不 fork protocol。
- **#191 取样**：新部署日志一对锚点 `transcription_deadline duration=` 与结束 `耗时`；≥5 条且含 ≥1h recorder://；失败/截尾样本排除；max×1.2 向上取整 0.5。不 SSH 产日志（另开取样卡）。
- **#199 blocked**：仓内工作流未接 publish；豁免仍有效。不跨仓改 registry。
