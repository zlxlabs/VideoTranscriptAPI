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
    && rm -rf /var/lib/apt/lists/*

# ============================================================
# 3. BBDown: download Linux build from GitHub Release
#    File name includes version (e.g. BBDown_1.6.3_20240814_linux-x64.zip),
#    so we use GitHub API to find the correct download URL.
# ============================================================
RUN BBDOWN_URL=$(curl -s https://api.github.com/repos/nilaoda/BBDown/releases/latest \
    | grep -o '"browser_download_url": *"[^"]*linux-x64\.zip"' \
    | head -1 | cut -d'"' -f4) \
    && echo "Downloading BBDown from: ${BBDOWN_URL}" \
    && curl -L -o /tmp/BBDown.zip "${BBDOWN_URL}" \
    && mkdir -p /app/BBDown \
    && unzip /tmp/BBDown.zip -d /app/BBDown/ \
    && chmod +x /app/BBDown/BBDown \
    && rm /tmp/BBDown.zip

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
