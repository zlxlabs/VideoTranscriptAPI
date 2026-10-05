# Docker 部署方案设计

> 日期：2026-03-13
> 状态：已确认，待实施

---

## 目标

为 VideoTranscriptApi 项目添加 Docker 支持，降低部署门槛，方便开源用户快速使用。

## 设计决策

| 决策项 | 选择 | 理由 |
|--------|------|------|
| 镜像范围 | 主服务 + 工具依赖（ffmpeg、BBDown、yt-dlp） | CapsWriter/FunASR 作为外部服务连接 |
| 基础镜像 | python:3.11-slim | 稳定，apt 安装 ffmpeg 方便，兼容 numpy 等 C 扩展 |
| BBDown 获取方式 | 构建时从 GitHub Release 自动下载 | 降低使用门槛 |
| 配置注入 | 配置文件挂载（bind mount） | 配置项多且嵌套深，环境变量不合适 |
| 数据持久化 | bind mount 映射文件夹 | 用户可直接访问缓存、日志、转录结果 |
| 发布策略 | 先手动发布到 Docker Hub，后续加 GitHub Actions | 先验证流程再自动化 |
| Docker Hub 镜像名 | `zj1123581321/video-transcript-api` | — |

## 文件结构

```
project-root/
├── docker/
│   ├── Dockerfile              # 主镜像构建文件
│   ├── docker-compose.yml      # 编排文件
│   └── .dockerignore           # 构建上下文排除
```

## Dockerfile 设计

```dockerfile
FROM python:3.11-slim

# 1. 系统依赖：ffmpeg、unzip、curl
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg curl unzip \
    && rm -rf /var/lib/apt/lists/*

# 2. BBDown：从 GitHub Release 下载 Linux 版
RUN curl -L -o /tmp/BBDown.zip \
    https://github.com/nilaoda/BBDown/releases/latest/download/BBDown_linux-x64.zip \
    && unzip /tmp/BBDown.zip -d /app/BBDown/ \
    && chmod +x /app/BBDown/BBDown \
    && rm /tmp/BBDown.zip

# 3. Python 依赖：用 uv 安装
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

# 4. 项目源码
COPY src/ ./src/
COPY main.py ./

# 5. 目录预创建
RUN mkdir -p data/cache data/temp data/workspace data/logs

EXPOSE 8000
ENTRYPOINT ["uv", "run", "python", "main.py", "--start"]
```

**层缓存策略**：依赖安装在源码 COPY 之前，改代码不触发重装依赖。

## docker-compose.yml 设计

```yaml
services:
  video-transcript-api:
    image: zj1123581321/video-transcript-api:latest
    build:
      context: ..
      dockerfile: docker/Dockerfile
    ports:
      - "8000:8000"
    volumes:
      - ../config:/app/config
      - ../data:/app/data
    restart: unless-stopped
```

**使用方式**：
- 本地构建：`cd docker/ && docker compose up -d --build`
- 拉取镜像：`cd docker/ && docker compose up -d`

## Docker Hub 发布流程（手动）

```bash
# 构建
cd docker/
docker build -t zj1123581321/video-transcript-api:latest -f Dockerfile ..

# 推送
docker login
docker push zj1123581321/video-transcript-api:latest

# 带版本号（和 pyproject.toml 版本对齐）
docker tag zj1123581321/video-transcript-api:latest zj1123581321/video-transcript-api:0.1.0
docker push zj1123581321/video-transcript-api:0.1.0
```

## 测试方案

### 1. 基础功能验证
- [ ] 容器正常启动，API 响应 `http://localhost:8000`
- [ ] 配置文件挂载正确读取（auth_token 生效）
- [ ] 日志正常写入到映射的 `data/logs/`

### 2. BBDown B站下载验证
- [ ] 容器内 BBDown 可执行权限正常
- [ ] 提交 B 站视频转录任务，下载 → 转录完整流程
- [ ] 验证 .NET 运行时依赖（可能需要额外安装 `libicu`）

### 3. 外部服务连接
- [ ] 容器连通宿主机 CapsWriter（`ws://host.docker.internal:6016`）
- [ ] 容器连通宿主机 FunASR（`ws://host.docker.internal:8767`）
- [ ] LLM API 外网调用正常

### 4. 数据持久化验证
- [ ] 容器重启后缓存仍在
- [ ] 缓存命中逻辑正常

### 5. yt-dlp YouTube 下载验证
- [ ] 提交 YouTube 视频任务，下载正常
- [ ] cookie 文件挂载路径正确（如启用）

## 已识别风险

| 风险 | 影响 | 缓解措施 |
|------|------|---------|
| BBDown .NET 依赖缺失 | B站下载失败 | 构建时验证，必要时加装 `libicu` |
| 容器访问宿主机服务 | CapsWriter/FunASR 连接失败 | 文档说明 `host.docker.internal` 或 `network_mode: host` |
| BBDown GitHub Release 下载失败 | 构建中断 | 提供手动下载备选方案 |
