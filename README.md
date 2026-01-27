# 视频转录 API (Video Transcript API)

> 基于 Python 3.11+ 的异步视频转录服务，支持多平台下载、双引擎转录、智能文本处理和企业级功能集成。

[![Python](https://img.shields.io/badge/Python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.101+-green.svg)](https://fastapi.tiangolo.com/)
[![License](https://img.shields.io/badge/License-MIT%2B%20Commons%20Clause-yellow.svg)](LICENSE)

---

## 📋 目录

- [项目简介](#项目简介)
- [核心特性](#核心特性)
- [架构概览](#架构概览)
- [快速开始](#快速开始)
- [功能详解](#功能详解)
- [文档索引](#文档索引)
- [开发指南](#开发指南)
- [开源协议](#开源协议)

---

## 项目简介

本项目提供统一的视频/音频转录 API，支持从多个主流平台下载内容，并使用两种 ASR 引擎（CapsWriter-Offline / FunASR）进行语音识别。转录后的文本通过 LLM 进行智能校对、总结和说话人推断，同时内置缓存系统、风控机制和企业微信通知功能。

### 外部依赖
- [Tikhub API key，用于音视频解析下载。有 aff](https://user.tikhub.io/register?referral_code=YArXsaWi)
- [zj1123581321/funasr_spk_server: funasr server 对应暴露 api，支持音视频转写，分角色，自动合并相同人物的话。](https://github.com/zj1123581321/funasr_spk_server)
- [HaujetZhao/CapsWriter-Offline: CapsWriter 的离线版，一个好用的 PC 端的语音输入工具，支持热词、LLM处理。](https://github.com/HaujetZhao/CapsWriter-Offline)
- OpenAI 兼容的 API，比如 Deepseek , 量大管饱。

---

## 核心特性

### 🎯 多平台支持

项目使用工厂模式动态匹配下载器，支持以下平台：

| 平台 | 下载器类 | 特殊能力 |
|------|---------|---------|
| **YouTube** | YoutubeDownloader | 原生字幕、远程 API 服务器、yt-dlp 下载 |
| **Bilibili** | BilibiliDownloader | TikHub API、BBDown 工具支持 |
| **抖音** | DouyinDownloader | TikHub API 获取无水印流 |
| **小红书** | XiaohongshuDownloader | TikHub v3 接口 |
| **小宇宙播客** | XiaoyuzhouDownloader | 网页爬虫解析 |
| **通用链接** | GenericDownloader | 直接流式下载、断点续传 |

**工厂模式实现**：
```python
def create_downloader(url):
    # 依次尝试平台特定下载器
    platform_downloaders = [
        DouyinDownloader(),
        BilibiliDownloader(),
        XiaohongshuDownloader(),
        YoutubeDownloader(),
        XiaoyuzhouDownloader()
    ]
    for downloader in platform_downloaders:
        if downloader.can_handle(url):
            return downloader
    # 兜底：通用下载器
    return GenericDownloader()
```

### 🤖 双引擎转录

#### CapsWriter-Offline（通用转录）

**技术实现**：
- WebSocket 实时流式传输
- 音频分段处理：25s 片段 + 2s 重叠
- 自动格式转换（支持 MP3/WAV/M4A 等）
- 生成 FunASR 兼容格式的 JSON 输出
- 默认端口：6006

**配置参数**：
```json
{
  "capswriter": {
    "server_url": "ws://localhost:6006",
    "file_seg_duration": 25,
    "file_seg_overlap": 2,
    "max_retries": 5,
    "retry_delay": 3
  }
}
```

#### FunASR（说话人识别）

**技术实现**：
- WebSocket 连接管理（心跳间隔 60s）
- 支持分片上传（1MB/片）
- MD5 哈希校验，支持服务器缓存
- 队列状态查询（task_queued 消息）
- 说话人分离（Diarization）
- 默认端口：8767

**配置参数**：
```json
{
  "funasr_spk_server": {
    "server_url": "ws://localhost:8767",
    "max_retries": 3,
    "retry_delay": 5,
    "connection_timeout": 30
  }
}
```

### 🧠 智能文本处理

**核心功能**（基于 `EnhancedLLMProcessor`）：

1. **自动校对（Calibration）**
   - 修正语音识别中的同音字和语法错误
   - 标点符号规范化
   - 支持长文本自动分段并发处理
   - 配置模型：`calibrate_model`

2. **内容总结（Summary）**
   - 生成分段摘要或核心要点
   - 支持单说话人和多说话人模式
   - 配置模型：`summary_model`
   - 最小文本阈值：`min_summary_threshold`（默认 500 字符）

3. **说话人推断（Speaker Inference）**
   - 结合视频元数据推断真实姓名
   - 将匿名标识（`spk_0`, `spk_1`）映射为具体人名
   - 支持对话结构保留

4. **风险模型切换**
   - 自动检测敏感内容
   - 切换到专用风险模型：`risk_calibrate_model`、`risk_summary_model`
   - 配置开关：`enable_risk_model_selection`

**分段处理策略**：
- 触发阈值：`enable_threshold`（默认 20000 字符）
- 每段大小：`segment_size`（默认 8000 字符）
- 最大段大小：`max_segment_size`（默认 12000 字符）
- 并发数：`concurrent_workers`（默认 10）

**结构化校对**（带说话人识别时）：
- 单块最小长度：`min_chunk_length`（默认 800）
- 单块最大长度：`max_chunk_length`（默认 3000）
- 首选块长度：`preferred_chunk_length`（默认 2000）
- 质量验证：`enable_validation`（默认 true）

**JSON 输出模式**：
- 按模型名匹配输出模式：`mode_by_model`
- 支持 `json_object` 和 `json_schema` 两种模式
- 自动重试：`max_retries`（默认 2 次）

### 🏗️ 企业级功能

#### 智能缓存系统

**数据存储结构**：
- **SQLite 数据库**（`cache.db`）：
  - `video_cache` 表：平台、URL、标题、作者、媒体 ID、说话人标识、文件位置
  - `task_status` 表：任务 ID、查看令牌、状态、创建/完成时间
- **文件系统**：存储实际内容（转录文本、LLM 校对、LLM 总结）

**查询逻辑**：
```python
cache_data = cache_manager.get_cache(
    platform=platform,
    media_id=video_id,
    use_speaker_recognition=use_speaker_recognition
)
```

**智能缓存策略**：
- 当 `use_speaker_recognition=true` 时，查询带说话人识别的缓存
- 当 `use_speaker_recognition=false` 时，优先使用带说话人识别的缓存（信息更丰富）
- 自动验证文件完整性，删除无效记录

#### 多用户管理

**认证方式**：Bearer Token

**配置文件**（`config/users.json`）：
```json
{
  "users": {
    "sk-xxx": {
      "user_id": "user1",
      "name": "用户名",
      "enabled": true
    }
  }
}
```

**支持功能**：
- 用户启用/禁用：`enabled` 字段
- 单 Token 回退模式：不支持多用户时使用 `api.auth_token`
- API Key 脱敏显示：只显示前 8 位

#### 审计日志

**记录内容**：
- API 端点
- 请求时间、响应时间
- 处理耗时
- 状态码
- 用户 ID、API Key（脱敏）
- 视频地址、任务 ID

**查询接口**：
- `GET /api/audit/stats?days=30`：获取最近 N 天的统计
- `GET /api/audit/calls?limit=100`：获取最近 N 条调用记录

#### 企业微信通知

**基于 `wecom-notifier` 库**：

**核心特性**：
- 全局单例模式：统一频率控制（20 条/分钟）
- 超长文本自动分段
- URL 保护模式：避免被风控误处理
- 支持自定义 webhook（用户级覆盖）

**通知时机**（从代码确认）：
- 任务创建时（包含查看链接）
- 任务开始处理（`开始处理 - {engine_info}`）
- 缓存命中时（`使用已有缓存，含 LLM 结果`）
- 任务完成时（`【任务完成】`）

#### 风控系统

**敏感词管理**：
- 支持从远程 URL 动态加载敏感词库：`sensitive_word_urls`
- 本地缓存：`cache_file`
- 多策略脱敏：`summary`（整体替换）、`title`（前 6 字符）、`general`（移除所有）
- URL 豁免：自动识别并保留 URL

**文本脱敏策略**：
```python
text_sanitizer.sanitize(text, text_type)
# text_type 可选：
# - "summary": 如有敏感词则整体替换为"内容风险，请通过 url 查看"
# - "title": 移除敏感词后取前 6 字符
# - "author": 移除敏感词后取前 6 字符
# - "general": 移除所有敏感词
```

---

## 架构概览

### 系统架构

```
┌─────────────────────────────────────────────────────────────┐
│                     用户请求层                          │
├─────────────────────────────────────────────────────────────┤
│  FastAPI → verify_token → audit_logger → task_queue (异步) │
└────────────────────────┬────────────────────────────────────┘
                         │
         ┌───────────────┼───────────────┐
         │               │               │
    ┌────▼────┐    ┌────▼────┐    ┌──▼─────────┐
    │ 下载器  │    │ 转录器  │    │  LLM 处理器 │
    │  工厂   │    │ 双引擎   │    │  增强处理器  │
    └────┬────┘    └────┬────┘    └────┬──────┘
         │              │               │
         │              │               │
    ┌────▼──────────────▼──────────────▼────┐
    │         智能缓存系统                │
    │  (SQLite 元数据 + 文件系统)         │
    └─────────────────────────────────────┘
```

### 核心模块

| 模块 | 文件路径 | 职责 |
|------|---------|------|
| **API 服务** | `api/` | FastAPI 应用、路由、依赖注入 |
| **下载器** | `downloaders/` | 多平台内容获取、工厂模式 |
| **转录器** | `transcriber/` | 语音识别、说话人识别 |
| **LLM 引擎** | `utils/llm/` | 文本校对、总结、分段、结构化校对（已重构为模块化架构） |
| **缓存系统** | `utils/cache/` | 元数据存储、文件管理 |
| **通知系统** | `utils/notifications/` | 企业微信消息推送（WeComNotifier） |
| **风控模块** | `utils/risk_control/` | 敏感词检测、内容脱敏 |
| **用户管理** | `utils/accounts/` | 多用户鉴权、配置管理 |
| **审计日志** | `utils/logging/audit_logger.py` | API 调用追踪、统计 |

---

## 快速开始

### 环境要求

- **Python**: 3.11+
- **转录服务器**（二选一或同时部署）：
  - CapsWriter-Offline：默认端口 6006
  - FunASR：默认端口 8767
- **依赖工具**：FFmpeg（音频处理）、uv（包管理器）

### 安装步骤

```bash
# 1. 克隆仓库
git clone <repository-url>
cd video-transcript-api

# 2. 安装 uv（如果尚未安装）
curl -LsSf https://astral.sh/uv/install.sh | sh

# 3. 同步依赖（自动创建虚拟环境）
uv sync

# 4. 配置服务
cp config/config.example.jsonc config/config.jsonc
# 编辑 config/config.jsonc，填写必要配置：
# - api.auth_token
# - tikhub.api_key
# - llm.api_key（启用 LLM 功能时）
# - wechat.webhook（可选）

# 5. 启动服务
uv run python main.py --start
```

### 基本使用

```bash
# 1. 提交转录任务
curl -X POST "http://localhost:8000/api/transcribe" \
  -H "Authorization: Bearer your-auth-token" \
  -H "Content-Type: application/json" \
  -d '{
    "url": "https://www.xiaoyuzhoufm.com/episode/687893e0a12f9ff06a98a597",
    "use_speaker_recognition": true
  }'

# 响应:
{
  "code": 202,
  "message": "任务已提交",
  "data": {
    "task_id": "task_xxx",
    "view_token": "view_xxx"
  }
}

# 2. 查询任务状态
curl -X GET "http://localhost:8000/api/task/task_xxx" \
  -H "Authorization: Bearer your-auth-token"

# 响应:
{
  "code": 200,
  "message": "转录成功",
  "data": {
    "video_title": "...",
    "transcript": "..."
  }
}

# 3. 查看结果（Web 界面）
# 访问: http://localhost:8000/view/view_xxx

# 4. 导出结果
# 导出校对文本: http://localhost:8000/export/view_xxx/calibrated
# 导出总结: http://localhost:8000/export/view_xxx/summary
# 导出原始转录: http://localhost:8000/export/view_xxx/transcript
```

## Web 界面使用

### 📝 Web 任务提交

**访问地址**：`GET /add_task_by_web`

**功能说明**：
- 图形化界面提交视频转录任务
- 输入视频 URL
- 选择是否启用说话人识别
- 提交后获得任务 ID 和查看链接

**页面模板**：`src/web/templates/index.html`

### 👁 查看转录结果

**访问地址**：`GET /view/{view_token}`

**页面状态**：
| 状态 | 说明 | 模板 |
|------|------|------|
| `processing` | 任务正在处理中 | `processing.html` |
| `success` | 任务完成成功 | `transcript.html` |
| `failed` | 任务处理失败 | `error.html` |
| `file_cleaned` | 文件已被清理 | `cleaned.html` |

**成功页面展示内容**：
1. **转录统计**：
   - 原始转录字数
   - 校对文本字数
   - 总结文本字数

2. **LLM 配置信息**：
   - 校对模型（calibrate_model）
   - 总结模型（summary_model）
   - Reasoning Effort 参数
   - 风险降级标识（has_risk）

3. **内容区域**：
   - 📝 **内容总结**：LLM 生成的摘要（Markdown 格式）
   - ✨ **校对文本**：LLM 校对后的文本（支持说话人识别对话格式）
   - 浮动目录（TOC）：支持快速跳转

4. **导出按钮**：
   - 导出校对文本
   - 导出总结文本
   - 导出原始转录

### 📄 Raw 模式导出

**访问地址**：`GET /view/{view_token}?raw=calibrated`

**功能说明**：
- 直接返回纯文本，不渲染 HTML 页面
- Content-Type: `text/plain; charset=utf-8`
- 适合复制到第三方 AI 平台（如 ChatGPT、Claude）继续提问

**响应示例**：
```http
HTTP/1.1 200 OK
Content-Type: text/plain; charset=utf-8

[主持人] 今天我们请到了张三
[张三] 大家好，我是张三
...
```

### 📥 文件导出

**访问地址**：`GET /export/{view_token}/{export_type}`

**支持的导出类型**：
| 类型 | 文件路径 | 说明 |
|------|---------|------|
| `calibrated` | `llm_calibrated.txt` | LLM 校对后的文本 |
| `summary` | `llm_summary.txt` | LLM 生成的总结 |
| `transcript` | `transcript_funasr.json` 或 `transcript_capswriter.txt` | 原始转录（优先 FunASR，否则 CapsWriter） |

**文件名格式**：`{平台}_{标题}_{类型}.txt`

**响应头**：
```http
Content-Type: text/plain; charset=utf-8
Content-Disposition: inline; filename*=UTF-8''{encoded_filename}
X-Content-Type-Options: nosniff
```

**使用场景**：
- 下载校对文本进行人工审阅
- 下载总结文本快速了解内容
- 下载原始转录进行二次处理

---

## 运行测试

```bash
# 运行所有测试
uv run python scripts/run_tests.py

# 运行特定测试套件
uv run pytest tests/unit/
uv run pytest tests/integration/
uv run pytest tests/llm/
uv run pytest tests/cache/
```

---

## 功能详解

### API 端点

| 端点 | 方法 | 说明 |
|------|------|------|
| `/api/transcribe` | POST | 提交视频转录任务 |
| `/api/task/{task_id}` | GET | 查询任务处理状态 |
| `/api/audit/stats` | GET | 获取用户调用统计 |
| `/api/audit/calls` | GET | 获取最近调用记录 |
| `/api/users/profile` | GET | 获取当前用户信息 |
| `/add_task_by_web` | GET | Web任务提交页面 |
| `/view/{view_token}` | GET | 结果查看页面 |
| `/view/{view_token}?raw=calibrated` | GET | Raw 模式（纯文本输出） |
| `/export/{view_token}/{type}` | GET | 导出处理结果 |

### 请求参数

**`POST /api/transcribe`**：
```json
{
  "url": "视频URL（必填，实际下载地址）",
  "use_speaker_recognition": "是否使用说话人识别（默认 false）",
  "wechat_webhook": "企业微信 webhook 地址（可选）",
  "source_url": "原始视频URL（可选，用于解析平台和元数据）",
  "metadata_override": {
    "title": "视频标题（可选）",
    "description": "视频描述（可选）",
    "author": "视频作者（可选）"
  }
}
```

#### 参数说明

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `url` | string | 是 | 实际下载地址（可以是本地 HTTP 服务器地址） |
| `use_speaker_recognition` | boolean | 否 | 是否使用说话人识别功能（默认 false） |
| `wechat_webhook` | string | 否 | 企业微信 webhook 地址，用于发送通知 |
| `source_url` | string | 否 | 原始视频 URL，用于解析平台信息和元数据（适用于本地文件场景） |
| `metadata_override` | object | 否 | 元数据覆盖对象，用于补充或覆盖解析的元数据 |
| `metadata_override.title` | string | 否 | 视频标题 |
| `metadata_override.description` | string | 否 | 视频描述 |
| `metadata_override.author` | string | 否 | 视频作者 |

#### 使用场景示例

**场景 1：本地文件 + 原始元数据保留**

当你在本地下载了视频文件（如 YouTube 视频），并通过本地 HTTP 服务器暴露文件时，可以使用 `source_url` 保留原始平台信息：

```json
{
  "url": "http://localhost:8080/video.mp4",
  "source_url": "https://www.youtube.com/watch?v=abc123",
  "use_speaker_recognition": true
}
```

**场景 2：手动补充元数据**

当自动解析的元数据不准确时，可以使用 `metadata_override` 手动提供：

```json
{
  "url": "http://localhost:8080/video.mp4",
  "source_url": "https://www.youtube.com/watch?v=abc123",
  "metadata_override": {
    "title": "更准确的中文标题",
    "description": "补充的详细描述"
  }
}
```

**详细说明**：参见 [Source URL 和 Metadata Override 功能文档](docs/features/source_url_and_metadata_override.md)

### 响应状态码

| 状态码 | 含义 |
|-------|------|
| 200 | 任务成功完成 |
| 202 | 任务已提交或处理中 |
| 401 | 授权失败 |
| 404 | 任务不存在 |
| 500 | 服务器内部错误 |

---

## Web 界面使用

### 📝 Web 任务提交

**访问地址**：`GET /add_task_by_web`

**功能说明**：
- 图形化界面提交视频转录任务
- 输入视频 URL
- 选择是否启用说话人识别
- 提交后获得任务 ID 和查看链接

**页面模板**：`src/web/templates/index.html`

### 👁 查看转录结果

**访问地址**：`GET /view/{view_token}`

**页面状态**：
| 状态 | 说明 | 模板 |
|------|------|------|
| `processing` | 任务正在处理中 | `processing.html` |
| `success` | 任务完成成功 | `transcript.html` |
| `failed` | 任务处理失败 | `error.html` |
| `file_cleaned` | 文件已被清理 | `cleaned.html` |

**成功页面展示内容**：
1. **转录统计**：
   - 原始转录字数
   - 校对文本字数
   - 总结文本字数

2. **LLM 配置信息**：
   - 校对模型（calibrate_model）
   - 总结模型（summary_model）
   - Reasoning Effort 参数
   - 风险降级标识（has_risk）

3. **内容区域**：
   - 📝 **内容总结**：LLM 生成的摘要（Markdown 格式）
   - ✨ **校对文本**：LLM 校对后的文本（支持说话人识别对话格式）
   - 浮动目录（TOC）：支持快速跳转

4. **导出按钮**：
   - 导出校对文本
   - 导出总结文本
   - 导出原始转录

### 📄 Raw 模式导出

**访问地址**：`GET /view/{view_token}?raw=calibrated`

**功能说明**：
- 直接返回纯文本，不渲染 HTML 页面
- Content-Type: `text/plain; charset=utf-8`
- 适合复制到第三方 AI 平台（如 ChatGPT、Claude）继续提问

**响应示例**：
```http
HTTP/1.1 200 OK
Content-Type: text/plain; charset=utf-8

[主持人] 今天我们请到了张三
[张三] 大家好，我是张三
...
```

### 📥 文件导出

**访问地址**：`GET /export/{view_token}/{export_type}`

**支持的导出类型**：
| 类型 | 文件路径 | 说明 |
|------|---------|------|
| `calibrated` | `llm_calibrated.txt` | LLM 校对后的文本 |
| `summary` | `llm_summary.txt` | LLM 生成的总结 |
| `transcript` | `transcript_funasr.json` 或 `transcript_capswriter.txt` | 原始转录（优先 FunASR，否则 CapsWriter） |

**文件名格式**：`{平台}_{标题}_{类型}.txt`

**响应头**：
```http
Content-Type: text/plain; charset=utf-8
Content-Disposition: inline; filename*=UTF-8''{encoded_filename}
X-Content-Type-Options: nosniff
```

**使用场景**：
- 下载校对文本进行人工审阅
- 下载总结文本快速了解内容
- 下载原始转录进行二次处理

---

## 文档索引

项目文档中心位于 [docs/](docs/)，按用途分类：

### 📖 使用指南

- [企业微信通知配置](docs/guides/wechat_notification.md) - WeComNotifier 使用指南
- [多用户系统配置](docs/guides/multi_user_setup.md) - 用户管理、权限控制
- [API 使用指南](docs/guides/api/) - 各平台 API 详细说明

### 🔧 开发文档

- [LLM 工程指南](docs/development/llm/engineering_guide.md) - Prompt 优化、结构化输出
- [LLM 重构方案](docs/development/llm/refactoring_plan.md) - 模块化架构设计方案
- [LLM 重构完成报告](docs/development/llm/refactoring_completed.md) - 重构实施总结
- [架构优化方案](docs/development/architecture_optimization_plan.md) - 业务流程重构与迁移规划
- [架构优化完成报告（阶段一）](docs/development/architecture_optimization_completed_phase1.md) - 基础重构实施总结
- [架构优化完成报告（阶段二）](docs/development/architecture_optimization_completed_phase2.md) - 接口标准化实施总结
- [并发处理架构](docs/development/concurrency.md) - 双队列设计、性能优化
- [风控模块开发](docs/development/risk_control.md) - 敏感词管理、审核策略
- [日志系统指南](docs/development/logging.md) - Loguru 配置、日志分析

### ✨ 功能特性

- [原始导出功能](docs/features/raw_export.md) - 原始数据导出格式
- [平台适配开发](docs/development/platforms/) - 新平台接入指南

---

## 开发指南

### 项目结构

```
video-transcript-api/
├── src/
│   ├── video_transcript_api/
│   │   ├── api/              # FastAPI 服务
│   │   ├── downloaders/       # 平台下载器
│   │   ├── transcriber/       # 转录引擎
│   │   └── utils/            # 工具模块（按领域拆分）
├── tests/                    # 测试套件
│   ├── unit/                # 单元测试
│   ├── integration/          # 集成测试
│   ├── llm/                 # LLM 功能测试
│   ├── cache/               # 缓存功能测试
│   ├── features/            # 核心功能测试
│   └── platforms/           # 平台适配测试
├── docs/                     # 文档中心
│   ├── guides/              # 使用指南
│   ├── development/          # 开发文档
│   └── features/            # 功能特性
├── config/                   # 配置文件
├── scripts/                  # 工具脚本
└── main.py                   # 入口文件
```

### 添加新平台

1. 在 `src/video_transcript_api/downloaders/` 创建新的下载器类
2. 继承 `BaseDownloader`，实现：
   - `can_handle(url)`: 判断是否支持该 URL
   - `get_video_info(url)`: 提取视频元数据
   - `get_subtitle(url)`: 提取字幕（可选）
   - `download_file(url)`: 下载视频/音频
3. 在 `factory.py` 的 `platform_downloaders` 列表中注册
4. 添加对应的测试用例

### 代码规范

- **风格**：PEP 8，4 空格缩进
- **类型提示**：使用 Python 3.11+ 类型注解
- **文档**：Google 风格 docstring
- **日志**：使用 `video_transcript_api.utils.logging.setup_logger`
- **测试**：pytest + Mock，控制台输出禁止使用中文和 emoji

### 贡献流程

1. Fork 仓库并创建特性分支
2. 遵循代码规范，编写单元测试
3. 运行 `uv run pytest tests/` 确保测试通过
4. 提交 Pull Request，描述改动内容

---

## 开源协议

本项目基于 **MIT 协议 + Commons Clause 附加条款**开源：

- ✅ 允许：非商业用途的学习、修改、分发、自用
- ❌ 禁止：售卖本软件、提供付费服务、集成到商业产品中获利

详见 [LICENSE](LICENSE) 文件。

---

## 获取帮助

- 📖 [文档中心](docs/) - 详细的使用和开发文档
- 🐛 [Issues](../../issues) - 提交 Bug 或功能请求
- 💬 [Discussions](../../discussions) - 技术讨论

---

<p align="center">
  <i>Built with ❤️ by Video Transcript API Team</i>
</p>
