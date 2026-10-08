# 本仓真实浏览器回归

阻断入口：`npm run test:browser`。使用本地 Chromium、`e2e/` 锁定依赖、动态回环端口和测试 server 每次新建的临时 SQLite 数据库；每个 case 调用 `POST /__e2e__/reset` 独立重种。测试 server 仅在 `tests/browser_server.py`，不接生产配置、数据库或服务，不启动 ASR、LLM、通知服务。提交接口的 HTTP 响应为受控 fixture，因此本套测试验证现有页面与交互，不代表后台上传实现通过。

## 页面与覆盖

| 实际页面 / 路由 | 级别 | 阻断 spec 与可见断言 |
|---|---|---|
| `/`（`views.py` 的生产 HTML 常量） | 次要 smoke | `home page smoke`：标题及提交/历史链接 |
| `/add_task_by_web`（`src/web/static/index.html`） | 主路径 | `submission page sends...`：填令牌与 URL、点击提交、核对浏览器实际 POST payload 及成功状态；另有表单 smoke |
| `/static/history.html`（真实静态页面） | 主路径 | `history query renders...`：真实查询渲染 SQLite 种子、打开阅读页、已读状态变化；另有控件 smoke |
| `/view/{view_token}` 成功阅读（`src/web/templates/transcript.html`） | 主路径 | `public reading page...`：正文可见、展开导出控件、点击 TXT/HTML 导出并核对结果 |
| `/view/{view_token}` 处理中（`processing.html`） | 次要 smoke | `processing view smoke`：处理中状态与刷新控件 |
| `/view/{view_token}` 失败（`error.html`） | 次要 smoke | `failed view smoke`：错误标题和消息 |
| `/view/{view_token}` 文件清理（`cleaned.html`） | 次要 smoke | `cleaned view smoke`：清理状态和恢复指引 |

`/view/{token}?raw=...` 与 `?page=...` 是阅读页导出目的地，主路径测试通过真实链接点击断言。`/robots.txt`、`/sitemap.xml`、`/sw.js`、manifest 与 API 是资源/接口，不是额外页面；profile 路由是 API JSON，不是 UI。浏览器测试收集 console error、page error、request failed 及 HTTP 4xx/5xx，任一均使 case 失败。

## 运行及 CI

首次运行：`uv sync`、根目录 `npm ci`、`npm --prefix e2e ci`，并安装 Chromium：`cd e2e && npx playwright install chromium`。之后 `npm run test:browser`；保留的 Vitest 契约层单独以 `npm run test:web` 运行。缺 Chromium、服务无法启动或断言不符均非零；无 skip/retry。单一 spec、`fullyParallel: false`、`workers: 1` 避免共享 fixture 并发污染。

`.github/workflows/ui-browser.yml` 对所有 PR `opened`、`synchronize`、`reopened`、`ready_for_review`、`converted_to_draft` 事件在 `[self-hosted, ci]` 执行 Chromium 与 Vitest；也可 `workflow_dispatch`。本仓没有 push workflow。`.github/workflows/gate.yml` 的 caller 已设置 `has_ui: true`；引用的 `zlxlabs/gate/.github/workflows/gate-v2.yml@v2` 在实际 tag 的 `workflow_call.inputs` 声明 `has_ui: boolean`（tag `v2` 当前解析为 commit `805856b9c6a501145521e8cae06000dc669af8c6`，已通过 GitHub API 读取实际 workflow_call schema 确认）。gate-hub 台账仍待 #1393，不能据本仓 caller 推断登记已生效。

录屏证据不在本阻断 workflow。因无 `PWRS_REPORTER_TOKEN` 且 runner 发布通路未经验证，未接假 workflow；`AGENTS.md` 中有效期 90 天的 `ui-evidence-exempt:` 对应本仓接入待办 #199。该豁免只针对录屏，不豁免真实浏览器行为门禁。
