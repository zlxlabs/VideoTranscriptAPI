# D-r1 独立审查结论

failure-visibility: p1-found

对象固定为 be2e4fd6e92f8cb83a034def7b3856f337cf7290..f82e7bf1fcdc401c1d72c3ad141c2321a457e5ab。风险档为 personal。本轮审完 9 个文件，改动规模为 2020 行新增、58 行删除。

**结论：阻断交付。** 1 项 P1 在支持的 Chromium 中可复现：上传期间换文件，会把原文件的回执记到新文件名下，并清掉尚未上传的新文件。这是静默错误，符合 personal 档 P1 红线。其余 5 项为 P2，单独不阻断。

## Findings

### P1 — 在途换文件后回执串到新文件，且新选择被清除

- **违反：** 不变式 3。
- **位置：** src/web/static/js/app.js:1563-1596。POST 捕获了原文件和元数据，但回执落历史时用全局 selectedUploadFile.name，随后无条件调用 clearUploadFile()。
- **复现：** Chromium 中提交 first-file.wav，暂停 POST 后选择 replacement-file.wav，再返回原文件的 accepted 回执。请求元数据仍是 first-file.wav；本地历史却标成 replacement-file.wav，且新文件输入被清空。自有 scratch 复现见 fault-repro.spec.ts:53。
- **影响与阻断：** 实际转录内容与历史名称不一致；用户随后选择的文件没有提交，也没有失败提示。用户可能据错误名称判断转录内容，不能接受。
- **局部修法：** 在提交开始时保留该意图的文件与名称，回执只用该快照；只有当前选择仍是该文件时才清空。加浏览器回归测试覆盖上传期间换文件。

### P2 — receiving 回执被显示为受理成功并清除意图

- **违反：** 不变式 3、4。
- **位置：** src/web/static/js/app.js:1563-1596 对任意 truthy 回执都写历史、显示“任务已受理”、清除文件与 key。
- **证据：** 实际 FastAPI owner 查询可返回 state=receiving, task_id=null, view_token=null；自有 scratch 的真实 FastAPI/SQLite 查询通过。Chromium 注入该回执后观察到成功提示和文件输入清空，见 fault-repro.spec.ts:24。
- **影响：** 当前状态细节虽显示 receiving，主提示却宣称正式受理；未受理的意图无法按原 key 继续核实。现有单测只覆盖 accepted 回执，没有锁住 receiving/failed 的 UI 语义。
- **局部修法：** 仅 state=accepted 进入成功分支；其他状态显示对应状态，不清除文件或幂等 key。

### P2 — capabilities 未知或当前身份请求失败时仍允许提交

- **违反：** 不变式 2。
- **位置：** src/web/static/js/app.js:845-855, 1395-1422, 1502-1505。uploadCapabilities 为空时按启用处理；查询失败只记日志并保留旧值；提交仅拒绝明确的 enabled=false。
- **复现：** 支持的 Chromium 中保持 capabilities 请求未完成，按钮仍可用并发出 POST；先让身份 A 得到 enabled，再令身份 B 的请求返回 401，仍会用身份 B 的令牌发 POST。见 fault-repro.spec.ts:143,182。
- **影响与阻断：** 未取得当前身份的功能开关和文件限额时，客户端仍传文件；服务端会再次校验，但这违背前端 fail-closed 契约，也会让关闭/超限状态无法在提交前提示。P2，单独不阻断。
- **局部修法：** 每次身份变更/重新查询先清空旧 capabilities 并禁用提交；只有当前身份取得有效响应后再按开关和限额启用。

### P2 — HTTP 500 后不查询回执，用户再次点击会重发文件体

- **违反：** 不变式 3。
- **位置：** src/web/static/js/app.js:1540-1545 收到任何带状态码的错误便直接显示失败返回，没有按 key 查询。
- **真实失败路径：** src/video_transcript_api/api/routes/uploads.py:359-388 在 SQLite 接受并入队后发送通知；通知异常会让请求以 HTTP 500 结束。自有 scratch 的真实 FastAPI 测试确认 500 时记录已 accepted，owner 查询可读到 accepted；同 key 重放在服务端返回旧回执且 body 未被读取。
- **浏览器复现：** 500 后页面显示失败；再次点击发出相同 key 和相同原始文件字节的第二个 POST，没有 GET 查询。见 fault-repro.spec.ts:99。
- **影响与阻断：** 服务端幂等阻止重复建任务，但客户端违背未知受理只查询回执的约定，并再次传输文件体。P2，单独不阻断。
- **局部修法：** 对可能发生在接受之后的服务端错误，保留原意图并按 key 查询；核实失败前不再次发送文件体。

### P2 — 功能开关文档变量名与服务端不一致

- **违反：** 不变式 7 的文档承诺须与实际行为一致。
- **位置：** docs/features/local-upload.md:76 写 UPLOAD_ENABLED；src/video_transcript_api/api/routes/uploads.py:140-145 实际只检查 VTA_UPLOADS_ENABLED=="true"，并要求 limits 与 worker 就绪。
- **影响与阻断：** 按文档设置后上传仍保持关闭。P2，单独不阻断。
- **局部修法：** 将文档改为实际变量，并说明 limits 与 worker 条件。

### P2 — 新增不受支持平台与响应体解析 fallback

- **违反：** 不变式 7 明确要求现代浏览器路径，不增加 UUID、编码或未知响应 fallback；全局实现约定要求错误 fail fast。
- **位置：** src/web/static/js/app.js:411,444 与 src/web/static/history.html:1171 将 JSON 解析失败转成空对象；app.js:1237-1238 使用 unescape/encodeURIComponent 编码 fallback；app.js:1254-1259 在 crypto.randomUUID 不可用时以 Math.random 造 key。
- **影响与阻断：** 当前 Chromium 走主路径，未证明这些分支在目标浏览器中触发；fallback 让畸形响应或不支持环境继续运行，并降低幂等 key 熵。P2，单独不阻断。
- **局部修法：** 删除无批准的 fallback，让不受支持环境或非法响应显式失败。

## 不变式、代码与已提交测试对照

| 不变式 | 生产代码 | 已提交测试与覆盖边界 |
|---|---|---|
| 1. 六字段元数据、raw bytes、鉴权和幂等 key | app.js:421-451,1195-1223；服务端 uploads.py | e2e/tests/local-upload.spec.ts:40-115 检查真实 Chromium 请求头、六字段元数据和 body SHA；tests/integration/test_upload_intake.py::test_real_http_stream_sqlite_dispatch_idempotency_and_owner_stop 用 FastAPI/SQLite 检查真实入库 bytes。浏览器到服务端仍由不同测试覆盖。 |
| 2. capabilities 来自当前身份，未知/失败不等于启用 | app.js:845-867,1395-1422,1502-1511 | 有 enabled happy path；没有已提交测试锁住未返回、401、身份切换后的 fail-closed。scratch 浏览器故障测试复现反例。 |
| 3. 意图与 body/key 绑定，未知只查回执 | app.js:1523-1557 | src/web/tests/local-upload.test.js:286-326 锁住网络异常时查询；e2e/tests/local-upload.spec.ts:117-146 锁住重复点击只 POST 一次。未覆盖在途换文件或 HTTP 500 后只查询。 |
| 4. 只有 accepted 算正式受理，状态展示准确 | app.js:1563-1596；真实回执由 uploads.py:148-175,410-420 生成 | test_upload_intake.py::test_real_http_stream_sqlite_dispatch_idempotency_and_owner_stop 锁住 accepted producer；没有已提交浏览器测试锁住 receiving/failed 消费。scratch 真实 FastAPI 查询与浏览器故障测试分别验证 producer 和 consumer。 |
| 5. owner 历史、独立上传 token、停止分享与撤销后拒读 | api/routes/audit.py:268-350；src/web/static/history.html:1185-1215,1325-1390 | test_upload_history_filters_owner_before_page_limit 锁住 owner 过滤、字段、revoke 状态；FastAPI 生命周期测试验证撤销后 view/export/read 拒绝；E2E local-upload.spec.ts:148-205 锁 UI。E2E server 是 Python/SQLite fixture，不是真实 FastAPI。 |
| 6. 浏览器 picker/drop、页面和实际结果路径 | src/web/static/js/app.js 上传事件；history.html 历史消费 | 三个 E2E 用真实 Chromium，但 API 来自 tests/browser_server.py fixture；上传仅覆盖 file-input setInputFiles，未覆盖 drop、键盘和小屏行为。FastAPI raw body、SQLite、queue/worker 在独立集成测试验证，未做一条浏览器直连 FastAPI 的全链测试。 |
| 7. 无额外 fallback/抽象，文档不高于已证行为 | app.js:411,444,1237-1259；history.html:1171；功能文档第 76 行 | 没有已提交测试约束这些 fallback 不存在；配置变量有代码对照，未有文档校验测试。 |

## 验证与边界

- npm run test:web：14 个文件、181 个测试通过。
- npm run test:browser：12 个 Chromium 测试通过。
- make test：退出码 0。
- 固定 H0 scratch：历史 FastAPI 测试通过；将 audit.py 的 upload_view_token producer 临时改为 None 后，该测试因 token 值业务断言失败。
- 固定 base scratch：复制 H0 生命周期测试并用 .get("upload_view_token") 反验；缺字段导致 AssertionError，不是导入错误或 KeyError。
- OCR 前置扫描为 skipped，原因 backup:deepseek=leg_timeout；本结论不把它记作 clean。
- 浏览器 E2E fixture 与真实 FastAPI 集成测试分层，未验证生产配置、生产部署或浏览器直连真实 FastAPI 的全链。
- 派发基线 CI 查询不可用（gh api request failed），继承红无法判定。
