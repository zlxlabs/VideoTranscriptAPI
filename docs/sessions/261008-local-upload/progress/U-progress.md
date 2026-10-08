# U 进度

## 里程碑 1：真实 Chromium 阻断入口（implementing）

已接入 `npm run test:browser`，用临时 SQLite 种子和动态端口驱动 Chromium 对提交、历史、公开阅读/导出及其他实际页面做行为/烟雾检查；9 条用例本地全绿。真实页面缺少 `sw.js` 时浏览器报告 404 console error，已补 test-server 路由并保留 console/request 失败观察；无产品代码改动。

决策：阻断层独立于 Vitest 和录屏层；`retries=0`、单 spec、`fullyParallel=false`，各用例 beforeEach 重新种子。下一步：提交/推送此入口并开 draft PR，然后接入阻断 CI、登记 has_ui caller 与录屏豁免文档。
