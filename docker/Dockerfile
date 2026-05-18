FROM python:3.11-slim

# ============================================================
# 1. ffmpeg: copy static binaries from well-maintained image
#    (avoids slow apt-get with 200+ dependencies)
# ============================================================
COPY --from=mwader/static-ffmpeg:latest /ffmpeg /usr/local/bin/ffmpeg
COPY --from=mwader/static-ffmpeg:latest /ffprobe /usr/local/bin/ffprobe

# ============================================================
# 2. System dependencies: curl, unzip (for BBDown download)
# ============================================================
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    unzip \
    git \
    libjemalloc2 \
    && rm -rf /var/lib/apt/lists/*

# jemalloc: 替代 glibc malloc，主动归还空闲内存给 OS，
# 避免长跑后 RSS 因 arena 碎片居高不下
ENV LD_PRELOAD=/usr/lib/x86_64-linux-gnu/libjemalloc.so.2

# ============================================================
# 3. BBDown: copy from previous image (avoids GitHub API network issues)
#    To update BBDown version, rebuild from source:
#    docker build --build-arg BBDOWN_FROM_SOURCE=1 ...
# ============================================================
COPY --from=ghcr.io/zj1123581321/video-transcript-api:latest /app/BBDown /app/BBDown

# ============================================================
# 4. Python package manager: uv
# ============================================================
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# ============================================================
# 5. Python dependencies (cached layer - before source copy)
# ============================================================
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

# ============================================================
# 6. Application source code
# ============================================================
COPY src/ ./src/
COPY main.py ./

# ============================================================
# 7. Create runtime directories (overridden by bind mount)
# ============================================================
RUN mkdir -p data/cache data/temp data/workspace data/logs config

# ============================================================
# 8. Runtime configuration
# ============================================================
EXPOSE 8000

ENTRYPOINT ["uv", "run", "python", "main.py", "--start"]
