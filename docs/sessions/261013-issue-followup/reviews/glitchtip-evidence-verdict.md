# #211 本地出站证据独立审查

- 固定审查对象：`1bc3b50e29296612c2d162582158b4abf71d3f43..a3fece5905202b55147fb68c5f856fd9743878ec`；H0 仅新增 `diagnostics/glitchtip-boundary.md` 70 行。
- 结论：本地四组核心结果经原始 producer bytes 独立复算成立；spec 2、4 的文档范围仍有 P2 缺口，另有一项私有 collector 判据局限。均不阻断本 verdict 提交；不代表 H0 文档满足全部验收项。
- OCR：`skipped`，完整 JSON `status=skipped`、`reason=no_reviewable_items`；docs-only skipped 不算 clean。人工全量审阅继续。
failure-visibility: p2-only

## 独立复算

- 原始私有 `probe.py` 为 40,840 bytes；sha256 `61baf708983ff50f9f1b5cd8b5f1cf9e2ac44eaaf3a1f60a44f4fb5d6dc9b381`，与卡面一致。未运行/修改它，也未重放；六个 `run-*` 均保留。
- 独立 review 物理 venv 由 Base 的 `uv.lock` 用 `uv sync --frozen --no-dev --no-install-project` 建立；`brotli 1.2.0` 解压真实 HTTP `Content-Encoding: br` body，再用 `sentry_sdk.envelope.Envelope.deserialize` 解析；核对 HTTP Content-Length、body SHA、解压后长度/SHA、item 声明长度及 `event.request.url`。
- 已知坏输入反验：真实 event body 截掉末尾 payload 字节后 SDK JSON 解析报 `JSONDecodeError`；把期望 URL 改成错误路径时身份校验报 `ValueError`；随后未改动的 producer bytes 重新解析为 1 个 event 且 URL 匹配。session / client_report 按 item type 计 0 event。结果及独立验证代码在 `~/.local/state/delegate/20261013-033417-big-codex-VideoTranscriptAPI/review-evidence/`。
- 原始请求时序：两组 normal 客户端收到 HTTP 200、body 18B，5 秒观察窗记录 0 POST；其后 shutdown 收到 sessions POST。两组 bad-header 客户端收到 0 bytes、无 status line、clean EOF；event POST 在请求结束前到达且 `records_before_observation=1`，不靠 shutdown/atexit 判阳性。bad-header 日志先写 access-log `200 OK`，随后 `Exception in ASGI application`；该日志不代表 TCP 发出了响应，原始 socket 结果为准。
- 异常原文 `Illegal header value b'line1\nline2'`；`ExceptionGroup` mechanism `starlette/handled=false/is_exception_group=true`，内含 `h11._util.LocalProtocolError` (`starlette/handled=false`)。event request URL 与各自 loopback bad-header 请求逐条相同；事件 `logger=null`、无 `logentry`，并含 Sentry ASGI `asgi.py:356 _sentry_wrapped_send` / `:363 _run_app` 与帧 `starlette/middleware/errors.py:161 _send`、`uvicorn/protocols/http/h11_impl.py:492 send`、`h11/_events.py:151`、`h11/_headers.py:166`、`h11/_util.py:91`。
- 四组 child-ready 快照均为 sentry-sdk 2.64.0、active、`HttpTransport` / `sentry_sdk.transport`、真实集成清单；Uvicorn 0.46.0 auto→h11，Starlette 0.50.0、h11 0.16.0；锁定并核对 zlx-ops-sdk 0.1.0。真实源树 `api/app.py` SHA 与 Base 相同；`create_app()` 先构造，lifespan startup 调 `init_observability()`，其调用 `zlx_ops_sdk.init()` 在线程内调 `sentry_sdk.init()`；SDK ready 快照在 lifespan 完成后、请求前。探针无手动 init/capture、无 ASGITransport；两个新增路由走真实 Uvicorn/h11/TCP。
- 合成配置来自 `config.example` 后写入私有 run 目录；用户为合成 `probe_owner`。SENTRY DSN、LLM/CapsWriter/Feishu 均 loopback；WeCom webhook 空、risk_control 与 YouTube cookie 关闭，ASR 健康探测只指向 localhost/loopback。配置样例里虽留有远程风险词 URL，但开关关闭，日志无下载路径；本轮未观察到真实通知、转录或外部服务请求。此处只证明本地样本。

## Spec 判定

| Spec | 判定 | 依据 |
| --- | --- | --- |
| 1 | PASS | 固定 Base 源树、真实 app/lifespan、SDK 实际 transport 与版本、TCP producer 均可核。 |
| 2 | FAIL | 指定四组结果均成立；但 H0 未单列 2 次较早运行（一次启动失败且无请求，一次 normal 200 且 shutdown sessions），也未澄清 line 55 的“无 POST”仅指观察窗。 |
| 3 | PASS | 9 个实际 POST body 经 Brotli + 官方 Envelope parser 独立复算；session/client_report 非 event；bad event 早于观察窗。client_report 不作为 dedupe 根因证明。 |
| 4 | FAIL | SDK 启用、integration、argv/env/cgroup 可核且配置合成；但 env-i 与 systemd 两组不是独立的应用环境对照，见 F1。 |
| 5 | PASS | 真实 GlitchTip、生产、CI、release 聚合及告警均明确 unknown；dedupe 只标推断，未升级成根因。 |
| 6 | PASS | Base..H0 只有 70 行诊断文档；本分支只新增本 verdict，无产品代码、测试、依赖、配置或 workflow 改动。 |

## Findings

- **F1 — P2，不阻断。** `diagnostics/glitchtip-boundary.md:26,38` / Spec 4：env-i 记录的 parent 环境键实际为 `HOME, LC_CTYPE, PATH`；四个有效组的 parent cgroup 都是同一个 delegate unit。systemd parent 有 51 个键及 `INVOCATION_ID`，env-i parent 没有；但两边 API child 都使用同一显式白名单、`INVOCATION_ID=None`、会话身份键泄漏为 0，且仍在同一 cgroup。现有数据支持“runner parent 环境不同、应用子进程环境相同”，不足以表述为两个独立的 app 环境证明。
- **F2 — P2，不阻断。** `diagnostics/glitchtip-boundary.md:33,38,53-58` / Spec 2、3：表中 normal 明列 shutdown 的 1 个 sessions POST，但证据清单写“无 POST”；实际是观察窗 0 POST、关停后 1 sessions POST。六个原始 run 中四组指定样本之外，还有一次 normal 200（同样仅关停 sessions）和一次 child 启动失败（无请求、无 POST）；应在口径中单列，避免把四组有效结果误读成全历史。
- **F3 — P2，不阻断。** 私有 `probe.py:282-296,167-217,937-1006` / Spec 3：consumer parser 遇真实 body 解析异常时只记 `parse_error`，不加入 `failures`；event 摘要也不校验 event URL 与本次 socket target。故其 selfcheck fixture 只锁 parser 函数，不足以单独证明真实 body 完整或请求归属。当前四组原始 bytes 已由本 verdict 的独立 SDK parser、长度检查和错误 URL 负向输入复核通过；不据此推翻本地观测，也不把 probe 的 `event_total` 单独当证据。

## 全量原始 POST body SHA-256

| Run | 原始 body / 完整 SHA-256 | 解出的 item payload / 完整 SHA-256 |
| --- | --- | --- |
| 早期 normal `1791860843-4002719` | 190B sessions `cd94ea2845941358a6771233ff55188bb9e53849455f55ef98a5f965a14b1c5d` | sessions 191B `1534105ab2ff6c0f67a117353bf354d663a099fa9edd986daa1338d9eefb7f4f` |
| env-i normal `1791860969-4094943` | 190B sessions `aaf609fc9c1266923adab2d082169edacc336dae8436c8cdc82e5038916be01c` | sessions 191B `2164f494f26b6b5f7ef86214553824c5fc4b0e7149b25542bd2d9aa8b5913cf6` |
| env-i bad `1791861005-4120125` | 5659B event `daf29408b2dc05447b805f8f210284c62db13e760e6d8834de4476e6b2151484`; 192B sessions `58e63648406e8afb0d16c6a81bf9b73b0c325211be8f8ce0b945b27cd5207f91`; 142B client_report `acd45ba9f55c70da805d27249ab6c816c8fbb778e42ae999e053c8a8d4035b92` | event 27079B `78178491272be43b135b22a0b289d5f77ac9c5499624f6604b2a96a472f7a265`; sessions 192B `a599041de114234b40386923d2da2f16eb06e486530499dbe8d70d6da1cd6fc4`; client_report 114B `e503f815fdb4b7f79f9f9bb7948ddc8ddd579c847dbb0d1720c74d3862fc874b` |
| systemd normal `1791861103-76313` | 191B sessions `17a2dabc732f12a714f9ba0aac497d5e212509c3996e36f7bf0d1e88e6672844` | sessions 191B `18173363056f5f9177e7fc7c58ce824e0d8cff290ada0f2906055b7e4854dcdc` |
| systemd bad `1791861169-222398` | 5659B event `cc474729ae9294eda8639c12ca8ede26de53d68b74d3cdc071f347d206712822`; 193B sessions `92b5b7f505f55af1f22a7c167c8a55857cfa6c385aa673fec96ce1971a16e79c`; 141B client_report `e6bef5c62f213ba9e474e76503dfad576c760150e086e763b409bf978965e3cb` | event 27078B `4d5aa2f701b70becb3d10d7e59a8c4862f3398fc47364b5776f5db32cae97e40`; sessions 192B `c73357eb287e2d2098ea3a2b32d616009a730a97831abd8f9cbc146341995b03`; client_report 113B `2ebb32410b1bd4968a5c1be4eee1ec282e180c2c40adeaadaf9d725ecac9d90d` |

## 四收尾问

1. **审查完成？** 是；仅固定 Base..H0，结论限本地合成链路。
2. **跑了什么验证？** OCR skipped；独立 `uv sync --frozen` 物理 venv + raw body SDK 解析/负向检查；未跑 pytest、make 或全组重放。
3. **提交与远端？** 完成后以本报告补入完整 commit SHA，并远端核对非空 ref。
4. **CI/生产/下一步？** 未查询 CI、未访问真实 GlitchTip或 issue 状态；仍 unknown。P2 findings 留给主脑裁定，本轮不改产品或证据源。
