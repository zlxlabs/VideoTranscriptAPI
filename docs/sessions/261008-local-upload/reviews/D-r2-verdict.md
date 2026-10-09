<!-- delegate-outcome: succeeded -->
failure-visibility: p1-found

# D-r2 独立审查结论

## 固定对象与结论

- 完整审查对象：`be2e4fd6e92f8cb83a034def7b3856f337cf7290..cabbf6b5b38922244372bffc8569bab98577edb4`。
- 增量四问对象：`f82e7bf1fcdc401c1d72c3ad141c2321a457e5ab..cabbf6b5b38922244372bffc8569bab98577edb4`。
- 结论：有一项 P1 会使未知受理意图丢失并允许重复提交，当前候选不能交付。另有 P2 的意图签名碰撞、未消费状态、无依据空数组兜底和自动化覆盖缺口。

## 增量四问

1. 增量是否仍是局部消费逻辑、同路径测试和文档？是。改动落在网页上传/历史消费及相应测试、说明。
2. 是否新增未经批准的抽象？未发现通用重试层、额外 API 或新平台；`currentUploadIntent.metadata` 是未消费的单用途状态，见 P2-3。
3. 状态、事实源或 fallback 是否无依据新增？有两处：意图状态的 404 处理会丢失唯一核实 key（P1）；历史过滤选项把缺失字段兜成空数组（P2-4）。
4. 是否留下双路径？没有发现并行编码/重试实现；未知提交仍走原 key 查询路径，但查询 404 会清意图，后续显式提交会另造 key（P1）。

## Findings

### P1 — 核实回执 404 会清掉未知受理意图（阻断交付）

位置：`src/web/static/js/app.js:1587-1601`，尤其 `1595-1598`。收到网络/5xx 未知结果后，代码保留 key 并设 `pendingVerification`；下一次点击通过 `getUploadReceipt` 查询。这个查询返回 404 时也进入同一个 4xx 分支，立即把 `currentUploadIntent` 置空。用户再次点击会生成新 key 并重新发送文件。

这违反不变式 3：未知受理必须保留原 key，只按 key 查询；核实不得重发 body 或换 key。实际 Chromium 临时探针复现了 POST 500、两次 owner receipt GET 404 后意图被清除，下一次点击发出第二个 POST 且 key 不同。可能造成同一文件重复入队/重复处理。

建议局部修复：区分 POST 的明确拒绝与 receipt 查询结果；receipt 404 应保留原意图和 key，并明确显示“当前未查到回执/状态未知”，不得在核实路径清意图。为这一分支添加持久的浏览器回归测试。

### P2 — `metaSignature` 的冒号拼接可碰撞（阻断交付）

位置：`src/web/static/js/app.js:1567-1578`。签名把文件属性、标题、source URL 等用冒号串联，字段值本身可含冒号。临时 Chromium 探针用相同文件，分别设置 `(title="T", source="https://example.com/path:https://example.org")` 和 `(title="T:https://example.com/path", source="https://example.org")`，两组不同参数得到相同签名。未知意图下第二组参数复用了旧 key 并查询旧回执，未建立新意图。

这违反不变式 3 的“参数变更产生新 key”。建议对固定字段对象按固定键序列化后比较，或逐字段比较；提交的浏览器测试应断言参数变更产生新 key，且不把新参数误绑到旧回执。

### P2 — 未使用的 `currentUploadIntent.metadata` 状态（不单独阻断）

位置：`src/web/static/js/app.js:1571-1576`。该字段被保存，但源码中没有读取方；实际 POST 使用局部 `metadataSnapshot`。它增加了一份看似权威但不起作用的状态，违反不变式 7 对新增状态须有消费者和测试锁的要求。删除该字段即可。

### P2 — 历史过滤选项对缺字段静默兜底（不单独阻断）

位置：`src/web/static/history.html:986-991`。`webhooks`、`platforms`、`authors` 均以 `|| []` 处理。实际 `GET /api/audit/filter-options` 成功响应在 `src/video_transcript_api/api/routes/audit.py:687-695` 总是提供三个数组，异常走 HTTP 错误；缺字段是响应契约破坏，不应伪装成成功的空选项。建议移除这三个默认值，让不符合契约的响应走可见错误路径。

### P2 — 必需的浏览器回归场景未锁定（阻断交付）

位置：`e2e/tests/local-upload.spec.ts:207-221`、`src/web/tests/local-upload.test.js:301-340`。名为“小屏与拖放”的 Playwright 用例实际通过 `setInputFiles` 选择文件，没有键盘文件选择或 DOM 拖放事件；已提交用例也没有锁定 receipt 404 保留 key、参数变化建立新 key、失败回执等关键路径。纯 helper/模拟 fetch 用例不能防止真实页面事件与请求意图脱节。

临时真实 Chromium 探针确认键盘文件选择与 DOM 拖放能产生 HTTP 请求，提交的字节长度和 SHA-256 与输入一致，metadata 的全部布尔选项保持 `false`；但该一次性探针不是仓库回归测试。建议补入正式浏览器测试并保留 producer 收到的原始字节/headers/metadata 断言。现有 Playwright 服务端是合成 fixture，不等同于真实 FastAPI；真实 FastAPI 生命周期测试单独覆盖其 receipt、owner history 和 revoke 行为，见下表。

## 关键不变式到代码/测试

| 不变式 | 当前实现/证据 | 结论 |
| --- | --- | --- |
| 1. 原始文件、Bearer、幂等键、六字段 metadata 和校验 | `src/web/static/js/app.js` 上传构造；`src/web/tests/local-upload.test.js` metadata 编码断言；`tests/integration/test_upload_intake.py` 实际 FastAPI intake | 独立检查通过；实际 browser producer 字节另有临时探针，非持久测试 |
| 2. capabilities 与认证状态 | `app.js` capabilities/认证消费；`src/web/tests/local-upload.test.js` | 本次定向及全套测试通过；未发现身份串用 |
| 3. 意图、一次提交、未知受理核实 | `app.js:1567-1613`；`local-upload.test.js` 有模拟请求路径 | P1 404 清 key；P2 签名碰撞；所需回归场景未全部提交 |
| 4. receipt、任务状态和公开 token | `tests/integration/test_upload_intake.py`；`test_local_upload_lifecycle.py` | 实际 FastAPI lifecycle 覆盖 accepted/owner receipt 与状态；浏览器 E2E 使用合成 fixture |
| 5. owner history、撤销及独立 token | `src/video_transcript_api/api/routes/audit.py`；`tests/integration/test_local_upload_lifecycle.py:536-578` | 实际路由断言 `view_token is None`、`upload_view_token` 有效、撤销后 token 为 None |
| 6. 真实浏览器生产行为 | `e2e/tests/local-upload.spec.ts`、`tests/browser_server.py` | Chromium 真实浏览器但服务端为 synthetic fixture；键盘/drop 与关键未知态目前只有临时探针，无持久回归锁 |
| 7. 无额外抽象/fallback/无消费者状态 | `app.js:1571-1576`；`history.html:986-991` | 见 P2-3、P2-4 |

## 验证与边界

- `npm run test:web && npm run test:browser`：通过，14 个 Vitest 文件 / 185 个测试，Chromium 14 项通过。浏览器 runner 提示使用 Python 3.11 环境并设置 `UV_NO_SYNC`；其测试服务端为 synthetic fixture。
- 临时真实 Chromium 探针：复现 P1 和 P2；另验证键盘 picker、DOM drop、提交字节 SHA-256/长度、全部 false 选项。探针只在一次性 scratch 中运行，未写入仓库。
- `pytest -q tests/integration/test_local_upload_lifecycle.py`：8 项通过。全套 `make test` 在仓库 `.python-version` 指定的 Python 3.11 环境通过，记录 3821 pass、3 skip、0 fail、0 error、`[100%]`。第一次误用 Python 3.12 时依赖 `numpy==1.24.3` 因 `distutils` 缺失在 pytest 前构建失败；切换到项目 3.11 环境后通过。
- base 反验：在官方 scratch 的 `be2e4fd` 上运行新增的 history token 断言，得到预期的业务断言差异 `None == upload token`，不是导入错误或 `KeyError`。
- OCR 前置扫描按规定只运行一次，结果 `skipped`，原因 `backup:deepseek=leg_timeout`；因此 OCR 不能作为 clean 证据。私有证据：`/tmp/vta-upload-D-r2-261009-evidence.aCrvzE/ocr.stdout.json`、`ocr.stderr.txt`。
- 派发时主干基线查询不可用（`gh api request failed`），继承红无法判定；没有声称 CI 主审通过。草稿 PR 中主审 job 的 skip 不能作为通过证据。
- 所有验证使用隔离 scratch；review worktree 源码相对 H1 无改动。此次 review 不证明生产配置、完整 SDK 或部署链路行为。

## 交付建议

修复 P1 的 receipt 404 意图保留、P2 的 metadata 碰撞，并补齐阻断级浏览器回归场景后再复审。P2-3、P2-4 可同批做局部减法。除上述问题外，本次没有发现需扩大到后端或生产的变更。
