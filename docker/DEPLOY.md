# 部署流程与入口链路

## D3 自动部署（主链路）

push `main` 后自动完成构建与部署：`.github/workflows/deploy.yml` 调用 `zlxlabs/ci-templates` 的 `build-deploy.yml@v2`（self-hosted runner）：构建镜像（`docker/Dockerfile`，构建上下文为仓根）→ 推送 ACR（不可变 12 位短 SHA tag）→ SSH 到 n305 → 健康探针通过后在部署目录 `/opt/media/VideoTranscriptAPI` 按本地 tag `video-transcript-api:latest` 切换容器，探针失败自动回滚上一健康版本。`paths-ignore` 排除 `**.md`、`docs/**`、`.github/**`——只改文档或 CI 不扇出生产部署；workflow 自身修复后用 `workflow_dispatch` 手动补发。

- 健康探针用 `http://localhost:8200/livez`（宿主 8200 → 容器 8000），不用 `/health`：`/livez` 是纯存活探针、恒 200、不依赖下游，专为部署探针设计；`/health` 的深度检查依赖转写后端等下游服务，下游抖动会把健康的新版本误判为不健康并触发无谓回滚。
- 在飞任务保护（忙锁门禁）已在 workflow 启用（`busy_lock_file` 指向部署目录 `.deploy-state/busy.lock`，超时 600s 延期）：服务侧 BusyLock 只读打开部署侧预建的锁文件并持共享 flock（服务侧永不自建锁文件——自建文件落在容器层时与宿主部署锁不同 inode，互斥静默失效），锁基础设施异常一律 fail-closed 拒绝新任务（HTTP 503），不存在「锁文件缺失自动创建后门禁空转」的降级路径——宿主锁文件缺失时 deploy.yml 的 pre-flight 对账即失败，自动部署 fail-closed 延期、人工介入（首次人工部署步骤含 `touch busy.lock`）；事后对账失败还会自动回滚到 `.deploy-state/last_verified_good` 记录的最近验证好镜像。部署窗口仍建议确认无长转写任务（事故背景：2026-10-15 手动部署打断在飞转写任务）。
- 部署目录的 compose 必须与本仓种子同形态（本地 tag）：ci-templates 部署脚本从 ACR 拉取不可变 SHA 镜像后在本机 retag 成 `video-transcript-api:latest`，镜像对账也认这个本地名；旧的 digest-env 形态（registry digest env 占位）与之不兼容，首次自动部署前需先确认部署目录已换成本仓种子。
- **跨槽位层缓存与内网 registry**：`deploy.yml` 配置了 `local_registry`（`zlx-vm-work-i7-ci-runner.taile9071.ts.net:5001`，#290）。ci-templates 在多 slot self-hosted runner 构建时走 `docker buildx` 并通过 registry `--cache-from/--cache-to` 共享构建层缓存（含 apt 依赖与 python venv），同时镜像优先经 Tailscale 内网推拉，大幅降低依赖海外拉取与构建耗时。
- **外部镜像 digest 锁定与升级**：`docker/Dockerfile` 对所有外部基础镜像与工具镜像（`python:3.11-slim`、`mwader/static-ffmpeg`、`ghcr.io/astral-sh/uv`）均使用 `<tag>@sha256:<digest>` 锁定（#289），避免海外 upstream 浮动 tag 破坏层缓存或因意外变更导致构建抖动。升级依赖时，须先人工查证或拉取新镜像 digest，在 Dockerfile 显式更新对应 digest 注解与引用。

## 旧手动链路（已退役）

接入 D3 前的手动部署脚本 `docker/pull_and_deploy.sh` 已于 2026-10-15 删除：它以同目录种子为模板做部署目录自迁移，要求 digest-env 形态，与 D3 的本地 tag 形态种子互斥，D3 上位后失去存在意义（锁旧脚本行为的测试随退）。`docker/push_to_ghcr.sh` 保留，仅作 ghcr 公开镜像构建口：从干净主干构建 `ghcr.io/<owner>/video-transcript-api:<主干12位短SHA>` 并推送，不部署。

自动链路不可用时的灾备兜底（在部署目录手工执行，不再依赖仓内脚本；部署目标见 `docker/deploy_targets.json`）。灾备同样尊重忙锁、使用排他锁，超时失败属预期（等在飞任务清空而非强行恢复，避免打断转写）。

日常灾备直接重建容器（compose 用宿主已有的本地 tag `video-transcript-api:latest`，该 tag 由每次 D3 部署 retag 维持；**不要用 `docker compose pull`**——本地 tag 形态下它会去 registry 找名为 `video-transcript-api` 的镜像，不存在）：

```bash
flock -w 600 -x .deploy-state/busy.lock -c "docker compose up -d"
```

需要钉定版本时，从 ACR 手动拉取指定 SHA 并 retag 到本地 tag（ACR 全路径 = ci-templates 默认 registry/namespace + `image_name`，与本卡 deploy.yml 的实际值一致），同样带忙锁：

```bash
docker pull crpi-0vsre5argteykh9m.cn-guangzhou.personal.cr.aliyuncs.com/zlx-personal/video-transcript-api:<git-sha> \
  && docker tag crpi-0vsre5argteykh9m.cn-guangzhou.personal.cr.aliyuncs.com/zlx-personal/video-transcript-api:<git-sha> video-transcript-api:latest \
  && flock -w 600 -x .deploy-state/busy.lock -c "docker compose up -d"
```

入口地址、Tailscale 直连信息等基础设施细节维护在私有部署记忆中，不写入本公开仓。

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
