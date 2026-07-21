# 视频转录 API (Video Transcript API)

> 基于 Python 3.11+ 的异步视频转录服务，支持多平台下载、双引擎转录、智能文本处理和企业级功能集成。

[![Python](https://img.shields.io/badge/Python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.101+-green.svg)](https://fastapi.tiangolo.com/)
[![License](https://img.shields.io/badge/License-PolyForm%20Noncommercial%201.0.0-yellow.svg)](LICENSE)

开发契机和玩法分享：[LLM 吞噬一切，我用 AI 长出来的那些工具](https://mp.weixin.qq.com/s/w8VnWJcUp5VkD5J-fYCUrg)

![转录结果页面](docs/images/overview.jpg)

---

## 核心特性

- **多平台支持**：YouTube、Bilibili、抖音、小红书、小宇宙播客、Apple Podcast，工厂模式自动匹配下载器
- **双引擎转录**：CapsWriter-Offline（通用转录）+ FunASR（说话人识别）
- **智能文本处理**：LLM 自动校对 ASR 错误、专有名词纠错、按说话人采样+置信度降级的说话人推断、内容总结
- **处理深度可控**：`processing_options` 开关按任务控制是否校对/总结，分层缓存产物只增不减，重复请求自动复用已有层
- **诚实状态模型**：校对（full/partial/none/disabled）与总结（generated/skipped_short/failed/pending/disabled）状态全链路透传，不再用占位字符串掩盖失败
- **企业级功能**：SQLite + 文件系统双层缓存、多用户管理、审计日志（含 LLM token 用量统计）、多渠道通知（企业微信 + 飞书）、任务历史浏览器
- **风控系统**：敏感词检测、多策略文本脱敏、风险模型自动切换

## 外部依赖

- [Tikhub API key，用于音视频解析下载。有 aff](https://user.tikhub.io/register?referral_code=YArXsaWi)
- [funasr_spk_server：funasr server 对应暴露 api，支持音视频转写，分角色，自动合并相同人物的话。](https://github.com/zj1123581321/funasr_spk_server)
- [CapsWriter-Offline：CapsWriter 的离线版，一个好用的 PC 端的语音输入工具，支持热词、LLM处理。](https://github.com/HaujetZhao/CapsWriter-Offline)
- [youtube_download_api：YouTube 视频下载服务，作为 yt-dlp 的可选替代后端。](https://github.com/zj1123581321/youtube_download_api)（可选）
- MediaResolverAPI：短视频 URL → 无水印直链 + 元数据的集中解析服务，可选地接管抖音/小红书解析（可选，见[使用指南](docs/guides/media_resolver.md)）。
- OpenAI 兼容的 API，比如 Deepseek，量大管饱。

---

## 快速开始

### 环境要求

- Python 3.11+
- FFmpeg
- 转录服务器（CapsWriter / FunASR 二选一或同时部署）

### 本地安装

```bash
# 克隆仓库
git clone <repository-url>
cd video-transcript-api

# 安装依赖（使用 uv）
curl -LsSf https://astral.sh/uv/install.sh | sh
uv sync

# 配置服务
cp config/config.example.jsonc config/config.jsonc
# 编辑 config.jsonc，填写 api.auth_token、tikhub.api_key 等
# 可选：抖音/小红书改走 MediaResolverAPI 集中解析，设
#   downloaders.use_media_resolver=true 并配置 media_resolver 段
#   （使用指南：docs/guides/media_resolver.md）

# 启动
uv run python main.py --start
```

可在启动前执行无副作用配置预检；该命令不会连接外部服务、迁移数据库或启动线程：

```bash
uv run python main.py --check-config --config config/config.jsonc
```

### Docker 部署

```bash
# 准备配置
cp config/config.example.jsonc config/config.jsonc

# 本地构建并启动（使用固定的 dev 标签，不用于生产部署）
cd docker/
docker compose up -d --build
```

**Docker 镜像**：[`ghcr.io/zj1123581321/video-transcript-api`](https://ghcr.io/zj1123581321/video-transcript-api)

镜像内置 ffmpeg、BBDown、yt-dlp，无需额外安装。

生产部署禁止使用 `latest`。构建脚本会拒绝包含已跟踪或未跟踪修改的脏工作区，只从干净提交以 12 位 Git SHA 生成唯一 tag；部署脚本拉取该 tag 后按同一镜像仓库解析并固定 registry digest。候选镜像会先运行 `--check-config`，失败时不重启当前服务，启动后健康检查失败则恢复上一个 digest：

```bash
./docker/push_to_ghcr.sh
# 在 docker/deploy_targets.json 指定的 n305:/opt/media/VideoTranscriptAPI 上执行：
./docker/pull_and_deploy.sh ghcr.io/zj1123581321/video-transcript-api:<git-sha>
```

本仓库只提供部署能力；脚本不会自行 SSH 或自动上线。服务器首次运行会从 `docker/docker-compose.deploy.yml` 生成根目录 `docker-compose.yml`，配置文件位于 `<deploy-dir>/config/config.jsonc`，成功使用的 digest 记录在 `<deploy-dir>/.deploy-image`。同一项目目录的部署由 `.deploy.lock` 串行化；所有 Compose 操作固定使用部署根目录作为 project directory，并与候选预检加载同一份根目录 `.env`。重启前还会确认现有 Compose 文件确实把服务渲染为候选 digest。旧 Compose 不兼容时会先备份为 `docker-compose.yml.pre-digest.bak`，再迁移到仓库模板；候选失败回滚时会恢复原 Compose，并叠加仅覆盖镜像的配置把旧版本固定到记录的 digest。首次切换硬化脚本时，旧容器即使由 tag 启动也会先按原仓库解析为可回滚 digest；若旧镜像还没有 Docker `HEALTHCHECK`，回滚验证会改用容器内 `/livez` 探测。候选镜像启动失败、健康检查失败、启动后脚本被中断或成功 digest 状态文件无法原子提交时，脚本都会恢复旧 digest 并确认旧版本重新健康后才退出。

> **注意**：CapsWriter / FunASR 需单独部署，配置中的服务地址不能用 `localhost`，需改为宿主机 IP 或 `host.docker.internal`。

---

## 基本用法

### 提交转录任务

```bash
curl -X POST "http://localhost:8000/api/transcribe" \
  -H "Authorization: Bearer your-auth-token" \
  -H "Content-Type: application/json" \
  -d '{
    "url": "https://www.youtube.com/watch?v=xxx",
    "use_speaker_recognition": true
  }'
```

### 只转录不校对/不总结

通过 `processing_options` 按任务控制处理深度，`calibrate`、`summarize`、`infer_speaker_names` 均默认 `true`（等价历史行为）：

```bash
curl -X POST "http://localhost:8000/api/transcribe" \
  -H "Authorization: Bearer your-auth-token" \
  -H "Content-Type: application/json" \
  -d '{
    "url": "https://www.youtube.com/watch?v=xxx",
    "processing_options": {
      "calibrate": false,
      "summarize": false,
      "infer_speaker_names": false
    }
  }'
```

三个开关全部为 `false` 时不会调用 LLM。分层缓存产物只增不减：后续若对同一视频提交 `calibrate: true` 或 `infer_speaker_names: true` 的请求，只会补跑缺失且有效性校验未通过的层，已存在的转录不会重新下载。完整语义见 [处理深度开关功能文档](docs/features/processing_options.md)。

### 查询任务状态

```bash
curl -X GET "http://localhost:8000/api/task/{task_id}" \
  -H "Authorization: Bearer your-auth-token"
```

### Web 界面

- **提交任务**：`GET /add_task_by_web`
- **查看结果**：`GET /view/{view_token}` — 不可猜测的公开只读分享 capability；写操作仍需认证
- **任务历史**：`GET /static/history.html` — 支持按日期、平台、频道、关键词搜索，已读追踪，摘要预览
- **导出文件**：`GET /export/{view_token}/{type}`（支持 `calibrated`、`summary`、`transcript`）

### API 端点一览

| 端点 | 方法 | 说明 |
|------|------|------|
| `/api/transcribe` | POST | 提交转录任务（支持 `processing_options` 处理深度开关） |
| `/api/task/{task_id}` | GET | 查询任务状态 |
| `/api/recalibrate` | POST | 重新校对（唯一强制重做例外，忽略分层缓存保护；总结缺失时仍会自动补跑，但单独重跑总结请用 `/api/resummarize`） |
| `/api/resummarize` | POST | 只重新生成总结（跳过下载、转录、校对和章节，复用已有校对文本） |
| `/api/audit/stats` | GET | 调用统计，含 LLM token 用量聚合（按阶段汇总 prompt/completion/total tokens） |
| `/api/audit/calls` | GET | 调用记录 |
| `/api/audit/history` | GET | audit.db 独立终态任务历史（状态仅支持 `success`、`failed`、`all`；支持过滤、分页、关键词搜索），含处理状态与 `content_expired` |
| `/api/audit/filter-options` | GET | 获取过滤选项（webhook/平台/频道列表） |
| `/api/audit/summary` | GET | 任务摘要预览（前 300 字），基于诚实状态模型返回 `summary_status` |
| `/api/users/profile` | GET | 当前用户信息 |
| `/view/{view_token}` | GET | 结果查看页 |
| `/view/{view_token}?raw=calibrated` | GET | 纯文本导出 |
| `/view/{view_token}?page=calibrated` | GET | HTML 页面导出 |
| `/export/{view_token}/{type}` | GET | 文件下载 |

更多 API 细节请参考 [功能文档](docs/)。

---

## 项目结构

```
video-transcript-api/
├── src/video_transcript_api/
│   ├── api/              # FastAPI 服务、路由、依赖注入
│   ├── downloaders/      # 多平台下载器（工厂模式）
│   ├── transcriber/      # 转录引擎（CapsWriter + FunASR）
│   ├── llm/              # LLM 处理引擎（协调器-处理器-核心组件）
│   ├── cache/            # 缓存系统（SQLite + 文件系统）
│   └── utils/            # 工具模块（日志、通知、风控、用户管理等）
├── tests/                # 测试套件
├── docs/                 # 详细文档
├── config/               # 配置文件
├── docker/               # Docker 部署文件
└── main.py               # 入口文件
```

---

## 文档

详细文档位于 [docs/](docs/) 目录：

- **架构设计**：[系统架构与模块详解](docs/architecture.md)
- **使用指南**：[多渠道通知（企微+飞书）](docs/guides/notification.md) · [多用户系统](docs/guides/multi_user_setup.md) · [抖音/小红书 MediaResolverAPI 集成](docs/guides/media_resolver.md)
- **API 指南**：[FunASR](docs/guides/api/funasr_spk_server_client_api.md) · [YouTube](docs/guides/api/youtube_client_guide.md) · [BBDown](docs/guides/api/bbdown_guide.md)
- **开发文档**：[LLM 工程指南](docs/development/llm/engineering_guide.md) · [并发处理](docs/development/concurrency.md) · [日志系统](docs/development/logging.md)
- **功能特性**：[处理深度开关（processing_options）](docs/features/processing_options.md) · [Raw/Page 导出](docs/features/raw_export.md) · [Download URL 与元数据覆盖](docs/features/source_url_and_metadata_override.md)

---

## 测试

```bash
uv run python scripts/run_tests.py     # 运行所有测试
uv run pytest tests/unit/              # 单元测试
uv run pytest tests/integration/       # 集成测试
```

---

## 开源协议

基于 **[PolyForm Noncommercial License 1.0.0](https://polyformproject.org/licenses/noncommercial/1.0.0)** 开源。允许任何**非商业用途**的使用、学习、修改和分发；**禁止一切商业用途**（包括企业内部用于盈利业务、对外售卖或商业集成）。学术、教育、公益、政府等非营利机构的使用视为许可范围内。详见 [LICENSE](LICENSE)。
