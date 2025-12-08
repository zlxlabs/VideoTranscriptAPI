# 视频转录API

一个基于Python的视频转录API服务，支持从多个平台（抖音、Bilibili、小红书、YouTube）下载视频并转录为文字。支持两种转录引擎：CapsWriter-Offline 和 FunASR，可选择是否启用说话人识别功能。

## 功能特点

- 提供API接口，接收视频URL，返回视频的转录文本
- 支持多种平台：抖音、Bilibili、小红书、YouTube、小宇宙播客
- **双转录引擎支持**：
  - **CapsWriter-Offline**：高效的通用语音转录
  - **FunASR**：支持说话人识别的转录服务
- 支持两种获取转录方式：
  - 直接下载平台提供的字幕（如YouTube）
  - 下载视频/音频，发送至转录服务器进行处理
- **可选说话人识别功能**：自动区分不同说话人
- 支持并发处理多个转录任务
- 提供企业微信通知功能，实时通知任务状态
- 完善的日志系统，方便问题排查
- LLM集成：支持转录文本的校对和总结
- **智能缓存系统**：基于SQLite数据库的缓存管理，支持自动清理和完整性验证

## 项目结构

```
video_transcript_api/
├── src/                          # 源代码目录
│   ├── video_transcript_api/     # 主要包目录
│   │   ├── __init__.py
│   │   ├── api/                  # API服务模块（FastAPI）
│   │   │   ├── __init__.py
│   │   │   ├── app.py            # FastAPI 应用装配、生命周期钩子
│   │   │   ├── context.py        # 共享依赖（配置、缓存、队列、模板）
│   │   │   ├── routes/           # REST/视图路由拆分
│   │   │   ├── services/         # 转录/LLM 任务处理逻辑
│   │   │   └── server.py         # CLI 启动入口（uvicorn.run）
│   │   ├── downloaders/          # 视频下载模块
│   │   │   ├── __init__.py
│   │   │   ├── base.py           # 下载器基类
│   │   │   ├── douyin.py         # 抖音下载器
│   │   │   ├── bilibili.py       # B站下载器
│   │   │   ├── xiaohongshu.py    # 小红书下载器
│   │   │   ├── youtube.py        # YouTube下载器
│   │   │   ├── xiaoyuzhou.py     # 小宇宙播客下载器
│   │   │   ├── generic.py        # 通用下载器
│   │   │   └── factory.py        # 下载器工厂
│   │   ├── transcriber/          # 视频转录模块
│   │   │   ├── __init__.py
│   │   │   ├── transcriber.py    # 转录器实现（基于CapsWriter-Offline）
│   │   │   ├── capswriter_client.py  # CapsWriter精简客户端
│   │   │   └── funasr_client.py  # FunASR说话人识别客户端
│   │   └── utils/                # 工具模块（按子领域拆分）
│   │       ├── __init__.py
│   │       ├── logging/          # 日志与审计
│   │       │   ├── __init__.py
│   │       │   ├── logger.py
│   │       │   └── audit_logger.py
│   │       ├── cache/            # 缓存管理
│   │       │   ├── __init__.py
│   │       │   ├── cache_manager.py
│   │       │   ├── cache_analyzer.py
│   │       │   └── metadata_cache.py
│   │       ├── llm/              # LLM 处理工具
│   │       │   ├── __init__.py
│   │       │   ├── llm.py
│   │       │   ├── llm_enhanced.py
│   │       │   ├── llm_segmented.py
│   │       │   ├── structured_calibrator.py
│   │       │   ├── text_segmentation.py
│   │       │   └── speaker_mapping.py
│   │       ├── rendering/        # Markdown/对话渲染
│   │       │   ├── __init__.py
│   │       │   ├── dialog_renderer.py
│   │       │   └── markdown_renderer.py
│   │       ├── notifications/    # 企业微信通知
│   │       │   ├── __init__.py
│   │       │   └── wechat.py
│   │       ├── accounts/         # 多用户管理
│   │       │   ├── __init__.py
│   │       │   └── user_manager.py
│   │       ├── timeutil/         # 时区与时间工具
│   │       │   ├── __init__.py
│   │       │   └── timezone_helper.py
│   │       └── risk_control/     # 文本风控
│   │           ├── __init__.py
│   │           ├── sensitive_words_manager.py
│   │           └── text_sanitizer.py
│   └── web/                      # Web模板资源
│       └── templates/            # HTML模板
├── tests/                        # 测试模块
│   ├── unit/                     # 单元测试
│   ├── integration/              # 集成测试
│   ├── performance/              # 性能测试
│   ├── manual/                   # 手动测试脚本
│   ├── llm/                      # LLM功能测试
│   ├── cache/                    # 缓存功能测试
│   ├── features/                 # 功能特性测试
│   ├── platforms/                # 平台相关测试
│   └── sample_files/             # 测试样本文件
├── scripts/                      # 工具脚本
│   ├── cleanup_cache.py          # 缓存清理脚本
│   └── run_tests.py              # 测试运行脚本
├── docs/                         # 文档目录
│   ├── api/                      # API文档
│   ├── development/              # 开发文档
│   ├── architecture/             # 架构文档
│   └── examples/                 # 示例文档
├── config/                       # 配置文件目录
│   ├── config.example.json       # 配置示例文件
│   └── config.json               # 实际配置文件
├── data/                         # 数据目录
│   ├── cache/                    # 缓存数据
│   ├── logs/                     # 日志文件
│   └── temp/                     # 临时文件
├── BBDown/                       # BBDown工具 
├── main.py                       # 主程序入口
├── requirements.txt              # 项目依赖
├── .gitignore                    # Git忽略规则
├── README.md                     # 项目说明
└── CLAUDE.md                     # 项目开发规范
```

## 安装与配置

> **架构提示**：FastAPI 入口拆分为 `api/app.py`（装配 + 生命周期）、`api/context.py`（依赖注入）、`api/routes/*`（REST/视图）、`api/services/transcription.py`（转录/LLM 队列）。如需二次开发，请相应扩展路由或服务，避免重新堆叠到单文件 `server.py`。

### 环境要求

- Python 3.8+
- **转录服务器**（二选一或同时部署）：
  - CapsWriter-Offline 服务器：通用语音转录
  - FunASR 服务器：支持说话人识别的转录
- 足够的磁盘空间用于存储临时视频文件
- FFmpeg（用于音频处理）

### 安装步骤

1. 克隆代码仓库

```bash
git clone <repository-url>
cd 视频转录API
```

2. 创建虚拟环境

```bash
python -m venv venv
source venv/bin/activate  # Linux/Mac
venv\Scripts\activate     # Windows
```

3. 安装依赖

```bash
pip install -r requirements.txt
```

4. 设置 BBDown 可执行权限（仅 macOS/Linux）

```bash
# macOS
chmod +x BBDown/BBDown_Mac

# Linux
chmod +x BBDown/BBDown
```

> **注意**：从 Git 克隆后，BBDown 可能缺少可执行权限，导致 `[Errno 13] Permission denied` 错误。

5. 启动转录服务器（需要单独部署）

根据需求启动相应的转录服务器：

**CapsWriter-Offline 服务器**（通用转录）：
- 需要单独部署，本项目只包含客户端
- 默认监听端口：6006
- 请参考 CapsWriter-Offline 项目文档启动服务器

**FunASR 服务器**（说话人识别转录）：
- 支持说话人识别功能
- 默认监听端口：8767
- 请参考 FunASR 项目文档启动服务器

6. 修改配置文件

编辑`config/config.json`配置文件，设置相关参数：

```json
{
  "api": {
    "port": 8000,
    "host": "0.0.0.0",
    "auth_token": "your_api_token_here"
  },
  "tikhub": {
    "api_key": "your_tikhub_api_key_here"
  },
  "capswriter": {
    "server_url": "ws://localhost:6006",
    "max_retries": 3,
    "retry_delay": 5
  },
  "funasr_spk_server": {
    "server_url": "ws://localhost:8767",
    "max_retries": 3,
    "retry_delay": 5,
    "connection_timeout": 30
  },
  "transcriber": {
    "default_engine": "capswriter",
    "use_speaker_recognition": false
  },
  // 其他配置...
}
```

### 配置说明

- `api`: API服务配置
  - `port`: 服务端口
  - `host`: 服务主机地址
  - `auth_token`: API访问令牌
- `tikhub`: TikHub API配置
  - `api_key`: TikHub API密钥
- `capswriter`: CapsWriter-Offline配置
  - `server_url`: CapsWriter-Offline服务器URL
  - `max_retries`: 最大重试次数
  - `retry_delay`: 重试延迟（秒）
- `funasr_spk_server`: FunASR说话人识别服务器配置
  - `server_url`: FunASR服务器URL
  - `max_retries`: 最大重试次数
  - `retry_delay`: 重试延迟（秒）
  - `connection_timeout`: 连接超时时间（秒）
- `transcriber`: 转录器配置
  - `default_engine`: 默认转录引擎（capswriter/funasr）
  - `use_speaker_recognition`: 是否默认启用说话人识别
- `concurrent`: 并发配置
  - `max_workers`: 最大并发任务数
  - `queue_size`: 队列大小
- `storage`: 存储配置
  - `temp_dir`: 临时文件目录
  - `cache_dir`: 智能缓存系统目录
  - `cache_retention_days`: 缓存保留天数（默认360天）
- `wechat`: 企业微信配置
  - `webhook`: 企业微信webhook地址
- `log`: 日志配置
  - `level`: 日志级别
  - `format`: 日志格式
  - `file`: 日志文件路径
  - `max_size`: 日志文件大小限制
  - `backup_count`: 日志文件备份数量
- `llm`: 大语言模型配置
  - `api_key`: LLM API密钥
  - `base_url`: LLM API基础URL
  - `calibrate_model`: 校对文本使用的模型
  - `summary_model`: 内容总结使用的模型
  - `max_retries`: 最大重试次数（默认2次）
  - `retry_delay`: 重试间隔秒数（默认5秒）

## 使用方法

### 启动API服务

```bash
python main.py --start
```

### 企业微信通知最佳实践

本项目使用 [`wecom-notifier`](docs/api/企微通知器-USAGE_GUIDE.md) 库实现企业微信通知功能。为确保频率控制和消息顺序的正确性，项目遵循以下最佳实践：

#### ✅ 全局单例模式

**核心原则**：整个应用只使用一个 `WeComNotifier` 实例，所有通知共享此实例。

**实现方式**：
1. **API 服务器启动时自动初始化**：在 `server.py` 的 `startup_event` 中调用 `init_global_notifier()`
2. **API 服务器关闭时自动清理**：在 `server.py` 的 `shutdown_event` 中调用 `shutdown_global_notifier()`
3. **测试环境自动管理**：在 `tests/conftest.py` 中为测试会话管理全局实例

#### 📝 代码使用规范

**正确的导入路径**（避免模块重复加载导致单例失效）：
```python
# ✅ 推荐：使用完整的包路径
from video_transcript_api.utils.notifications import WechatNotifier, send_long_text_wechat

# ✅ 或使用相对导入
from ..utils.notifications import WechatNotifier

# ❌ 避免：不完整的路径
from utils.wechat import WechatNotifier
```

**创建通知器实例**：
```python
# 创建实例时，自动使用全局共享的 WeComNotifier
notifier = WechatNotifier()  # 或传入自定义 webhook
notifier.send_text("消息内容")
```

#### 🧪 测试环境配置

测试框架已在 `tests/conftest.py` 中统一管理全局实例：
- 所有测试共享同一个 `WeComNotifier` 实例
- 测试会话开始时自动初始化，结束时自动清理
- 无需在单个测试文件中重复初始化

#### 🔍 为什么需要单例？

每个 `WeComNotifier` 实例会为每个 webhook 创建独立的：
- 工作线程（处理消息队列）
- 频率控制器（20条/分钟）

如果创建多个实例：
- ❌ 无法协调频率限制，容易触发服务端频控（45009错误）
- ❌ 多个线程并发发送，消息顺序无法保证
- ❌ 资源浪费（每个实例一个线程）

#### 📖 更多信息

详细文档请参考：[企微通知器使用指南](docs/api/企微通知器-USAGE_GUIDE.md)

### API使用示例

#### 请求转录

**基本转录**（使用默认引擎）：
```bash
curl -X POST "http://localhost:8000/api/transcribe" \
  -H "Authorization: Bearer your_api_token_here" \
  -H "Content-Type: application/json" \
  -d '{"url":"https://www.xiaoyuzhoufm.com/episode/687893e0a12f9ff06a98a597"}'
```

**启用说话人识别**：
```bash
curl -X POST "http://localhost:8000/api/transcribe" \
  -H "Authorization: Bearer your_api_token_here" \
  -H "Content-Type: application/json" \
  -d '{"url":"https://www.xiaoyuzhoufm.com/episode/687893e0a12f9ff06a98a597","use_speaker_recognition": true}'
```

**使用自定义企微webhook**：
```bash
curl -X POST "http://localhost:8000/api/transcribe" \
  -H "Authorization: Bearer your_api_token_here" \
  -H "Content-Type: application/json" \
  -d '{
    "url":"https://www.xiaoyuzhoufm.com/episode/687893e0a12f9ff06a98a597",
    "wechat_webhook":"https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=your-custom-key"
  }'
```

**指定转录引擎**：
```bash
curl -X POST "http://localhost:8000/api/transcribe" \
  -H "Authorization: Bearer your_api_token_here" \
  -H "Content-Type: application/json" \
  -d '{"url":"https://www.xiaoyuzhoufm.com/episode/687893e0a12f9ff06a98a597","engine":"funasr"}'
```

响应:

```json
{
  "code": 202,
  "message": "任务已提交",
  "data": {
    "task_id": "task_1"
  }
}
```

#### 查询任务状态

```bash
curl -X GET "http://localhost:8000/api/task/task_1" \
  -H "Authorization: Bearer your_api_token_here"
```

响应:

```json
{
  "code": 200,
  "message": "转录成功",
  "data": {
    "video_title": "视频标题",
    "author": "视频作者",
    "transcript": "视频的转录文本内容...",
    "srt_path": "./output/youtube_sample_id.srt",
    "lrc_path": "./output/youtube_sample_id.lrc",
    "json_path": "./output/youtube_sample_id.json"
  }
}
```

### 使用测试脚本

#### 测试单个URL

**基本测试**：
```bash
python tests/integration/test_url.py url "https://www.xiaoyuzhoufm.com/episode/687893e0a12f9ff06a98a597"
```

**测试说话人识别**：
```bash
python tests/integration/test_url.py url "https://www.xiaoyuzhoufm.com/episode/687893e0a12f9ff06a98a597" --speaker-recognition
```

#### 测试URL列表

```bash
python tests/integration/test_url.py url_list url_list.txt -o results.json
```

#### 测试音频文件

```bash
python tests/manual/test_transcribe.py path/to/audio/file.mp3
```

#### 运行自动化测试

```bash
# 运行单元测试
python -m pytest tests/unit/

# 运行所有测试
python run_tests.py
```

## 语音转文字服务说明

本项目支持两种语音转文字服务，可根据需求灵活选择：

### 🎯 CapsWriter-Offline
**适用场景**：通用语音转录，追求速度和稳定性
- 使用精简客户端（`capswriter_client.py`）
- 默认端口：`ws://localhost:6006`
- 特点：快速、稳定、资源消耗低
- 输出：纯文本转录结果

### 🎤 FunASR Speaker Recognition Server
**适用场景**：多人对话、会议记录、访谈等需要区分说话人的场景
- 使用专用客户端（`funasr_client.py`）
- 默认端口：`ws://localhost:8767`
- 特点：支持说话人识别、时间戳、情感分析
- 输出：带说话人标识的转录结果

### 🔄 智能选择机制

**自动选择**：
- 默认使用 CapsWriter-Offline 进行转录
- 当请求中 `use_speaker_recognition=true` 时，自动切换到 FunASR

**手动指定**：
- 通过 `engine` 参数指定：`capswriter` 或 `funasr`
- 可在配置文件中设置默认引擎

### 📋 转录流程

1. **接收请求**：API接收视频URL和转录参数
2. **下载处理**：从平台下载视频/音频文件
3. **引擎选择**：根据参数选择合适的转录引擎
4. **音频转录**：将文件发送给对应服务器处理
5. **结果处理**：处理转录结果，支持文本校对和总结
6. **返回结果**：返回格式化的转录文本和相关信息

### ⚙️ 配置示例

```json
{
  "transcriber": {
    "default_engine": "capswriter",
    "use_speaker_recognition": false
  },
  "capswriter": {
    "server_url": "ws://localhost:6006"
  },
  "funasr_spk_server": {
    "server_url": "ws://localhost:8767"
  }
}
```

## 智能缓存系统

### 缓存架构

项目采用基于 SQLite 数据库 + 文件系统的智能缓存架构：

**数据库存储（元数据）**：
- 平台信息（youtube/bilibili/douyin等）
- 视频URL、标题、作者、描述
- 媒体ID和说话人识别标识
- 文件位置和时间戳

**文件系统存储（实际内容）**：
```
cache_dir/
├── platform/
│   └── YYYY/
│       └── YYYYMM/
│           └── media_id/
│               ├── transcript_funasr.json      # FunASR转录结果
│               ├── transcript_capswriter.txt   # CapsWriter转录结果
│               ├── llm_calibrated.txt         # LLM校对文本
│               └── llm_summary.txt            # LLM总结文本
```

### 智能缓存特性

1. **智能查询逻辑**：
   - 当 `use_speaker_recognition=true` 时，只使用带说话人识别的缓存
   - 当 `use_speaker_recognition=false` 时，优先使用带说话人识别的缓存（信息更丰富）

2. **LLM结果缓存**：
   - 自动缓存LLM校对和总结结果
   - 再次请求时直接返回缓存结果，避免重复调用LLM API
   - 大幅提升响应速度，降低API成本

3. **自动完整性维护**：
   - 查询时自动检测文件完整性
   - 自动删除无效的数据库记录
   - 保持数据库与文件系统一致性

### 缓存管理

**手动清理缓存**：
```bash
# 清理超过保留期限的旧缓存，并验证完整性
python scripts/cleanup_cache.py
```

**配置缓存保留时间**：
```json
{
  "storage": {
    "cache_dir": "./data/cache",
    "cache_retention_days": 360
  }
}
```

**缓存统计查看**：
缓存系统提供详细的使用统计，包括：
- 总记录数和存储空间占用
- 各平台缓存分布
- 说话人识别功能使用情况

### 性能优势

- **首次请求**：下载 → 转录 → LLM处理 → 缓存保存
- **缓存命中**：直接返回转录和LLM结果（秒级响应）
- **API成本节省**：避免重复的LLM调用
- **存储优化**：自动清理和时间分层存储

## 运行测试

```bash
python scripts/run_tests.py
```

## 许可证

[License Name]

## 作者

[作者名]
