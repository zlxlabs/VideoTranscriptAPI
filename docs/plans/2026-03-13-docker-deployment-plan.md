# Docker 部署实施计划

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 为 VideoTranscriptApi 添加完整的 Docker 支持，包括 Dockerfile、docker-compose、.dockerignore，并发布到 Docker Hub。

**Architecture:** 单容器镜像内置 ffmpeg + BBDown + yt-dlp + 项目源码，通过 bind mount 挂载配置文件和数据目录。CapsWriter/FunASR/LLM 作为外部服务连接。

**Tech Stack:** Docker, docker-compose, python:3.11-slim, uv, ffmpeg, BBDown

**Design Doc:** `docs/plans/2026-03-13-docker-deployment-design.md`

---

### Task 1: 创建 .dockerignore

**Files:**
- Create: `docker/.dockerignore`

**Step 1: 创建 .dockerignore 文件**

```
# Git
.git
.gitignore

# Python cache
__pycache__
*.py[cod]
*.egg-info
.pytest_cache
.mypy_cache
.ruff_cache

# Virtual env
.venv
venv
env

# IDE
.vscode
.idea
.DS_Store
Thumbs.db

# Data (runtime, not build)
data/cache
data/temp
data/workspace
data/logs
data/debug
data/risk_control

# Media files
*.mp3
*.mp4
*.m4a
*.m4s
*.wav
*.flac

# Config (mounted at runtime)
config/config.jsonc
config/users.json

# Tests
tests/
test_output/

# Docs
docs/
README.md

# BBDown binaries (Linux version downloaded during build)
BBDown/

# Debug files
debug_*
error_*
temp_*
tmp_*
output/
logs/
*nul
```

**Step 2: 验证文件已创建**

Run: `cat docker/.dockerignore | head -5`
Expected: 显示文件前 5 行

**Step 3: Commit**

```bash
git add docker/.dockerignore
git commit -m "chore: add .dockerignore for Docker build context"
```

---

### Task 2: 创建 Dockerfile

**Files:**
- Create: `docker/Dockerfile`

**Step 1: 创建 Dockerfile**

```dockerfile
FROM python:3.11-slim

# ============================================================
# 1. System dependencies: ffmpeg, curl, unzip
# ============================================================
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    curl \
    unzip \
    && rm -rf /var/lib/apt/lists/*

# ============================================================
# 2. BBDown: download Linux build from GitHub Release
#    BBDown is a .NET AOT build, no runtime needed for linux-x64
# ============================================================
RUN curl -L -o /tmp/BBDown.zip \
    https://github.com/nilaoda/BBDown/releases/latest/download/BBDown_linux-x64.zip \
    && mkdir -p /app/BBDown \
    && unzip /tmp/BBDown.zip -d /app/BBDown/ \
    && chmod +x /app/BBDown/BBDown \
    && rm /tmp/BBDown.zip

# ============================================================
# 3. Python package manager: uv
# ============================================================
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# ============================================================
# 4. Python dependencies (cached layer - before source copy)
# ============================================================
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

# ============================================================
# 5. Application source code
# ============================================================
COPY src/ ./src/
COPY main.py ./

# ============================================================
# 6. Create runtime directories (overridden by bind mount)
# ============================================================
RUN mkdir -p data/cache data/temp data/workspace data/logs config

# ============================================================
# 7. Runtime configuration
# ============================================================
EXPOSE 8000

ENTRYPOINT ["uv", "run", "python", "main.py", "--start"]
```

**Step 2: 验证语法**

Run: `cd docker/ && docker build --check -f Dockerfile .. 2>&1 || echo "docker build --check not supported, skip"`

如果 `--check` 不支持，跳过此步。

**Step 3: Commit**

```bash
git add docker/Dockerfile
git commit -m "feat: add Dockerfile for containerized deployment"
```

---

### Task 3: 创建 docker-compose.yml

**Files:**
- Create: `docker/docker-compose.yml`

**Step 1: 创建 docker-compose.yml**

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
      # Configuration files (user must prepare before starting)
      - ../config:/app/config
      # Persistent data: cache, logs, transcription results
      - ../data:/app/data
    restart: unless-stopped
```

**Step 2: 验证 YAML 语法**

Run: `cd docker/ && docker compose config 2>&1 | head -20`
Expected: 输出解析后的 compose 配置，无报错

**Step 3: Commit**

```bash
git add docker/docker-compose.yml
git commit -m "feat: add docker-compose.yml for service orchestration"
```

---

### Task 4: 更新 .gitignore

**Files:**
- Modify: `.gitignore`

**Step 1: 在 .gitignore 末尾追加 Docker 相关规则**

在文件末尾添加：

```
# Docker
docker/data/
```

注意：不要忽略 `docker/` 目录本身，Dockerfile、docker-compose.yml、.dockerignore 都需要提交。

**Step 2: 验证**

Run: `tail -3 .gitignore`
Expected: 显示新增的 Docker 规则

**Step 3: Commit**

```bash
git add .gitignore
git commit -m "chore: add Docker-related entries to .gitignore"
```

---

### Task 5: 构建镜像并验证基础启动

**Files:** 无新文件，纯验证

**Step 1: 构建镜像**

Run: `cd docker/ && docker compose build 2>&1`
Expected: 构建成功，无报错。关注以下几点：
- ffmpeg 安装成功
- BBDown 下载和解压成功
- uv sync 安装所有 Python 依赖成功
- 无 Python 编译错误（numpy 等）

**Step 2: 验证 BBDown 可执行**

Run: `docker run --rm zj1123581321/video-transcript-api:latest /app/BBDown/BBDown --version 2>&1 || echo "BBDown failed"`

Expected: 输出 BBDown 版本号。如果失败，可能需要在 Dockerfile 中加装 `libicu-dev`。

**Step 3: 验证 ffmpeg 可用**

Run: `docker run --rm --entrypoint ffmpeg zj1123581321/video-transcript-api:latest -version 2>&1 | head -3`
Expected: 输出 ffmpeg 版本信息

**Step 4: 验证容器启动（需要配置文件）**

准备 `config/config.jsonc`（从 `config.example.jsonc` 复制并填写 auth_token），然后：

Run: `cd docker/ && docker compose up -d && sleep 3 && curl -s http://localhost:8000/ && docker compose down`
Expected: 容器启动成功，API 有响应（可能是 404 或欢迎页，说明服务在运行）

**Step 5: 如果 BBDown 验证失败，修复 Dockerfile**

在 Dockerfile 的 apt-get 行添加 `libicu-dev`：
```dockerfile
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg curl unzip libicu-dev \
    && rm -rf /var/lib/apt/lists/*
```

重新构建并验证。

---

### Task 6: BBDown B站下载端到端验证

**Files:** 无新文件，纯验证

**前置条件：**
- 配置文件中 `tikhub.api_key` 已填写
- 宿主机上 CapsWriter 或 FunASR 已启动

**Step 1: 启动容器**

注意：如果 CapsWriter/FunASR 跑在宿主机上，配置文件中的 `localhost` 需要改为 `host.docker.internal`（Docker Desktop）或宿主机实际 IP。

Run: `cd docker/ && docker compose up -d`

**Step 2: 提交 B 站转录任务**

Run:
```bash
curl -X POST "http://localhost:8000/api/transcribe" \
  -H "Authorization: Bearer your-auth-token" \
  -H "Content-Type: application/json" \
  -d '{"url": "https://www.bilibili.com/video/BV1xx411c7mD", "use_speaker_recognition": false}'
```

Expected: 返回 202，包含 task_id

**Step 3: 查询任务状态**

Run: `curl -s "http://localhost:8000/api/task/{task_id}" -H "Authorization: Bearer your-auth-token"`

Expected: 任务最终状态为 success。如果 BBDown 下载失败，检查容器日志：
```bash
docker compose logs -f video-transcript-api
```

**Step 4: 验证缓存文件生成**

Run: `ls -la ../data/cache/bilibili/`
Expected: 有对应的缓存目录和转录文件

**Step 5: 清理**

Run: `cd docker/ && docker compose down`

---

### Task 7: 手动推送到 Docker Hub

**Files:** 无新文件

**Step 1: 登录 Docker Hub**

Run: `docker login -u zj1123581321`
Expected: Login Succeeded

**Step 2: 构建带标签的镜像**

Run:
```bash
cd docker/
docker build -t zj1123581321/video-transcript-api:latest -t zj1123581321/video-transcript-api:0.1.0 -f Dockerfile ..
```

**Step 3: 推送镜像**

Run:
```bash
docker push zj1123581321/video-transcript-api:latest
docker push zj1123581321/video-transcript-api:0.1.0
```

Expected: 推送成功

**Step 4: 验证拉取**

Run:
```bash
docker rmi zj1123581321/video-transcript-api:latest
docker pull zj1123581321/video-transcript-api:latest
```

Expected: 拉取成功

---

### Task 8: 更新 README 添加 Docker 使用说明

**Files:**
- Modify: `README.md`

**Step 1: 在「快速开始」章节后添加 Docker 部署说明**

在 `## 快速开始` 章节的安装步骤之后，添加：

```markdown
### Docker 部署

```bash
# 1. 克隆仓库
git clone <repository-url>
cd video-transcript-api

# 2. 准备配置文件
cp config/config.example.jsonc config/config.jsonc
# 编辑 config/config.jsonc，填写必要配置

# 3. 启动服务
cd docker/
docker compose up -d

# 4. 查看日志
docker compose logs -f
```

**注意事项**：
- 镜像内已包含 ffmpeg、BBDown、yt-dlp，无需额外安装
- CapsWriter / FunASR 需要单独部署，配置文件中的地址不能使用 `localhost`，需改为宿主机 IP 或 `host.docker.internal`
- 数据目录 `data/` 和配置目录 `config/` 通过 bind mount 映射到宿主机
```

**Step 2: Commit**

```bash
git add README.md
git commit -m "docs: add Docker deployment instructions to README"
```

---

## 实施顺序总结

| Task | 内容 | 类型 | 预计时间 |
|------|------|------|---------|
| 1 | 创建 .dockerignore | 文件创建 | 2 min |
| 2 | 创建 Dockerfile | 文件创建 | 3 min |
| 3 | 创建 docker-compose.yml | 文件创建 | 2 min |
| 4 | 更新 .gitignore | 文件修改 | 1 min |
| 5 | 构建镜像 + 基础验证 | 验证 | 10 min |
| 6 | BBDown B站下载端到端验证 | 验证 | 10 min |
| 7 | 推送 Docker Hub | 发布 | 5 min |
| 8 | 更新 README | 文档 | 3 min |

Task 1-4 可并行创建文件，Task 5-6 是关键验证步骤，Task 7-8 收尾。
