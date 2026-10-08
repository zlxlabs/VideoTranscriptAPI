# U 浏览器测试与 CI 增量审查 verdict

审查范围：`2e6f71a7f2ab06e7ed48d9270bbcc38aaa00b570..43f4db771dd850062ccedc2d05ccf394b25ca9d3`（H0，PR #200），14 个文件，688 additions / 2 deletions。仅审此差分。

## 裁决

**不阻断交付。** H0 的真实 Chromium 回归和 Vitest 步骤均已在 PR CI 执行并成功。未发现 P1 或会阻断合并的问题；有一项 P2 结构性意见，影响很小，可接受不修。

failure-visibility: clean

## 规格核对

| 规格 | 判定与证据 |
|---|---|
| 真浏览器、保留 Vitest、阻断全 spec | 满足。根 `test:web` 原命令仍是 `vitest run`；`test:browser` 调用独立 Playwright Chromium。`playwright.config.ts` 使用 `testMatch: **/*.spec.ts`、`forbidOnly`、`retries: 0`、`workers: 1`、`fullyParallel: false`。H0 CI 实际运行 Vitest 13 files / 170 tests 与 Chromium 9 tests，均通过。 |
| 真实页面及用户操作 | 满足。`tests/browser_server.py` 读取生产首页 HTML 常量、静态页面文件和真实 Jinja 模板；`pages.spec.ts:39-93` 在 Chromium 实际提交并断言 POST JSON/成功状态，查询历史并打开阅读页，阅读正文并点击 TXT/HTML 导出。其余首页、提交、历史及处理中/失败/清理页各有 smoke。后台响应是隔离 fixture，不代表真实上传或 ASR 后台通过。 |
| 异常与请求失败可见 | 满足。`pages.spec.ts:9-23, 33-36` 收集 console error、page error、request failed、HTTP 4xx/5xx，并在每个 case 后断言为空；reset 非 2xx 会抛错；Playwright webServer 启动超时/失败使测试命令非零。 |
| SQLite、端口、服务隔离 | 满足。Node 入口从回环端口 0 分配端口并将 `VTA_BROWSER_PORT` 传给 Playwright；`reuseExistingServer: false`。Python 服务绑定 `127.0.0.1`，每次启动新建 `TemporaryDirectory` 与 SQLite；`/__e2e__/reset` 只存在于 `tests/browser_server.py`。CI 为单 worker，case 前重种。未发现固定时间参与断言或访问生产配置/ASR/通知的路径。 |
| CI 接线与模型 gate | 满足。PR #200 的真实 run [37762745649](https://github.com/zlxlabs/VideoTranscriptAPI/actions/runs/37762745649) 在精确 H0 SHA 上执行 `npm run test:web` 与 `npm run test:browser`；`chromium-regression=SUCCESS`。PR 仍为 draft，`gate / primary=SKIPPED`，不能算模型主审通过；`gate / quality=SUCCESS`。原 gate 保留且 `has_ui: true`。浏览器 workflow 的真实事件为 `pull_request`，无 push 触发。 |
| 录屏与上游登记 | 满足豁免边界。GitHub secret 名称查询确认 `PWRS_REPORTER_TOKEN` 不存在；本仓 issue #199 为 OPEN，AGENTS 豁免日期是 2026-10-08；上游 gate-hub #1393 为 OPEN。没有假录屏 workflow/链接；豁免不覆盖浏览器测试。 |

## 动态证据与限制

- 权威 H0 CI 日志显示 Chromium 9/9（1 worker），Vitest 13/13 files、170/170 tests；PR 状态检查中的 `gate / primary` 为 `SKIPPED`。主干基线在派发时不可用，故继承红无法判定；H0 本次相关步骤均成功。
- 审查树没有 `node_modules`、`e2e/node_modules` 或 `.venv`，未安装依赖；按卡面使用上述同 SHA GitHub CI 作为测试证据。
- 对根 `npm run test:browser` 入口做了独立子进程负对照：`/tmp/vta-u-cli-probe.9jiVHa/child-payload.txt` 保存实际子进程 payload，断言 cwd 为 `e2e/`、动态端口在有效范围、argv 为 `playwright test`；已知子进程退出码 23 经入口原样传出，判据通过。浏览器请求 payload 则由真实 Chromium spec 对实际 POST 的 `postDataJSON()` 断言。
- OCR 前置已调用 `ocr-review`，background 1243 bytes，单腿 180s/总预算 480s。完整 JSON envelope 解析成功但 `status=skipped`，reason=`primary=leg_timeout; backup:deepseek=leg_timeout`，findings 为空不代表扫过或 clean。证据：`/tmp/vta-u-review1-envelope.pMjEJj.json`；本 verdict 的 clean 仅表示本轮独立审查未发现 failure-visibility 缺陷。

## 非阻断意见与未覆盖范围

- **P2，非阻断** — `e2e/reset-backend.ts:3`：该包装函数只有一个调用点（`e2e/tests/pages.spec.ts:30`），仅转发 reset POST 并检查状态码，没有第二个消费者；内联也能保持相同 fail-fast 行为。后果是多一个文件/跳转点，没有运行正确性风险；按单消费者抽象约束记为可接受的非阻断清理项。
- 受控 HTTP fixture 仅证明既有页面和交互；本轮不证明上传后端、ASR、通知或完整 16 合同。上游 `has_ui` registry 仍待 #1393；录屏发布链路仍待 #199。不得将 draft primary 的 skipped 描述为主审通过。
