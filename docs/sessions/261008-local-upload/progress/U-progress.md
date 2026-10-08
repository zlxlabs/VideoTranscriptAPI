# U 进度

## 里程碑 1：真实 Chromium 阻断入口（implementing）

已接入 `npm run test:browser`，用临时 SQLite 种子和动态端口驱动 Chromium 对提交、历史、公开阅读/导出及其他实际页面做行为/烟雾检查；9 条用例本地全绿。真实页面缺少 `sw.js` 时浏览器报告 404 console error，已补 test-server 路由并保留 console/request 失败观察；无产品代码改动。

决策：阻断层独立于 Vitest 和录屏层；`retries=0`、单 spec、`fullyParallel=false`，各用例 beforeEach 重新种子。下一步：入口 commit `665e84eb` 已推送并开 draft PR #200；继续接入阻断 CI、登记 has_ui caller 与录屏豁免文档。

## 里程碑 2：CI、覆盖文档与录屏例外（implementing）

新增 PR 阻断 workflow，保留现有 gate caller 且改 `has_ui: true`；实际查询 `zlxlabs/gate` 的 `v2` workflow_call schema 确认布尔输入存在。PWRS secret 名称检查结果为 absent，因此未创建录屏 workflow，按 90 天约定写入豁免并开本仓 issue #199；上游台账 #1393 仍 open。Chromium 9/9、Vitest 170/170 通过；将控件选择器临时改为不存在值会得到 `expect(locator).toBeVisible()` AssertionError，将 test-only URL 改为不存在路径会由 HTTP 404/console 404 观察器触发 `toEqual` AssertionError，恢复后重新全绿。唯一下一步：提交并推送 CI 接线单元，核对 PR 的真实 run/job conclusion 与浏览器 workflow 结果。
