# 部署流程与入口链路

镜像构建与部署由两个脚本完成：

- `push_to_ghcr.sh`：从干净主干构建 `ghcr.io/<owner>/video-transcript-api:<主干12位短SHA>` 并推送到 GHCR。
- `pull_and_deploy.sh`：在部署机上按**不可变 digest** 部署——先跑配置预检（`main.py --check-config`），再替换容器并等待健康检查，失败自动回滚到上一 digest；状态记录在部署目录 `.deploy-image`。

部署目标（服务器别名与目录）记录在 `docker/deploy_targets.json`；入口地址、Tailscale 直连信息等基础设施细节维护在私有部署记忆中，不写入本公开仓。

## 入口分层与 Cloudflare 请求体上限

公网入口经 Cloudflare 代理（Tunnel），**单请求 body 受 Cloudflare 套餐上限约束，超限的请求在边缘即被拒绝，不会到达源站**：

| Cloudflare 套餐 | 单请求 body 上限 |
| --- | --- |
| Free / Pro | 100 MB |
| Business | 200 MB |
| Enterprise | 默认 500 MB（可申请调整） |

2026-10-10 在公网入口实测：50 MB POST 正常穿过边缘到达源站（返回鉴权 401 JSON）；110 MB POST 被边缘直接返回 **HTML 413**（响应带 `server: cloudflare` 与 `cf-ray`），源站日志与数据库均无痕迹。

**实际可上传大小 = min(服务端 `upload_limits.max_file_mib`，入口链路上限)。** 需要上传大文件时使用不经 Cloudflare 代理的直连入口（VPN/内网直达源站端口，地址见私有部署记忆）；分块上传在本项目设计中明确排除。

前端表现：边缘 HTML 413 会被期望 JSON 的代码解析为 `Unexpected token '<'`，上传停在"状态待核实"；此时按契约只查询原 idempotency key 的回执、不重发文件体。判别方法：带 `cf-ray` 头的 4xx/5xx HTML 来自 Cloudflare 边缘而非应用。

## 浏览器安全上下文

`crypto.randomUUID()` 等 Web API 仅在安全上下文（HTTPS 或 `localhost`）可用。经纯 HTTP 直连入口打开页面时，上传幂等键生成已回退到 `crypto.getRandomValues` 手工构造 UUID v4（PR #218；`getRandomValues` 在非安全上下文同样可用）。新增入口形态前，先核对 secure-context-only API 清单。

## 上传功能开关与限额

- 开关 `VTA_UPLOADS_ENABLED`：部署域环境变量，经 env_file 注入、位于数据卷之外；缺失或非精确小写 `true` 即整体关闭。恢复数据卷不得覆盖该开关。
- 限额 `storage.upload_limits`（`max_file_mib` / `max_media_hours` / `receive_concurrency` / `upload_temp_budget_mib`）：**四项任一缺失或非法时，上传整体 503 拒绝**（fail-closed），不存在"部分限额"或无上限默认值。
