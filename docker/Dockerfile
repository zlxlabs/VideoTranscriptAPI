# 外部镜像均显式锁定 digest（#289）；升级依赖时须显式改 digest。
# BBDown 二进制来自项目私有镜像的已知好版本副本（ACR bbdown-base tag）。
# 开源用户如需自建须提供含 BBDown 的等效镜像（存量限制，非本卡引入）。
ARG BBDOWN_FROM=crpi-0vsre5argteykh9m.cn-guangzhou.personal.cr.aliyuncs.com/zlx-personal/video-transcript-api:bbdown-base
# static-ffmpeg 仅 latest/latest-amd64 指向该 digest，无独立语义版本 tag，保留 latest 并锁 digest
ARG FFMPEG_IMAGE=mwader/static-ffmpeg:latest@sha256:f698cee4f6d63a0ee5a80df40b093aae96a80c06e27836736b2347208bd13dc2
FROM ${BBDOWN_FROM} AS bbdown
FROM ${FFMPEG_IMAGE} AS ffmpeg

FROM python:3.11-slim@sha256:07d4824036af339fa1b6ef769cc99d9c10a141769538b52b3b597c84f7a7b535

# ============================================================
# 1. ffmpeg: copy static binaries from well-maintained image
#    (avoids slow apt-get with 200+ dependencies)
# ============================================================
COPY --from=ffmpeg /ffmpeg /usr/local/bin/ffmpeg
COPY --from=ffmpeg /ffprobe /usr/local/bin/ffprobe

# ============================================================
# 2. System dependencies: curl, unzip (for BBDown download)
# ============================================================
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    unzip \
    git \
    libjemalloc2 \
    fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/*

# jemalloc: 替代 glibc malloc，主动归还空闲内存给 OS，
# 避免长跑后 RSS 因 arena 碎片居高不下
ENV LD_PRELOAD=/usr/lib/x86_64-linux-gnu/libjemalloc.so.2

# ============================================================
# 3. BBDown: copy from previous image (avoids GitHub API network issues)
#    To update BBDown version, rebuild from source:
#    docker build --build-arg BBDOWN_FROM_SOURCE=1 ...
# ============================================================
COPY --from=bbdown /app/BBDown /app/BBDown

# ============================================================
# 4. Python package manager: uv
# ============================================================
COPY --from=ghcr.io/astral-sh/uv:0.13.7@sha256:96f40d3904aad8d0bd010539001d36c8e2188b981e1334471a90c4a1d93bb490 /uv /usr/local/bin/uv

# ============================================================
# 5. Python dependencies (cached layer - before source copy)
# ============================================================
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

# Runtime directories change rarely; keep them before application source.
RUN mkdir -p data/cache data/temp data/workspace data/logs config

# ============================================================
# 6. Application source code
# ============================================================
COPY src/ ./src/
COPY main.py ./

# ============================================================
# 8. GIT_SHA: 供 zlx-ops-sdk 拼 release=repo@<sha>，GlitchTip 按版本聚合错误
#    非 D3 服务由 push_to_ghcr.sh 传 --build-arg GIT_SHA
# ============================================================
ARG GIT_SHA=unknown
ENV GIT_SHA=$GIT_SHA

# ============================================================
# 9. Runtime configuration
# ============================================================
EXPOSE 8000

HEALTHCHECK --interval=10s --timeout=3s --start-period=20s --retries=6 \
  CMD curl --fail --silent http://127.0.0.1:8000/livez >/dev/null || exit 1

ENTRYPOINT ["uv", "run", "python", "main.py", "--start"]
