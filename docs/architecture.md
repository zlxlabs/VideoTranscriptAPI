# 系统架构

本文档详细介绍视频转录 API 的系统架构、核心模块设计和处理流程。

---

## 架构概览

```
┌─────────────────────────────────────────────────────────────┐
│                     用户请求层                              │
├─────────────────────────────────────────────────────────────┤
│  FastAPI → verify_token → audit_logger → task_queue (异步)  │
└────────────────────────┬────────────────────────────────────┘
                         │
         ┌───────────────┼───────────────┐
         │               │               │
    ┌────▼────┐    ┌────▼────┐    ┌──────▼─────┐
    │ 下载器   │    │ 转录器   │    │ LLM 处理器  │
    │  工厂    │    │ 双引擎   │    │  协调器     │
    │+URL解析  │    │         │    │ (模块化）    │
    └────┬────┘    └────┬────┘    └────┬───────┘
         │              │               │
    ┌────▼──────────────▼───────────────▼────┐
    │         智能缓存系统                    │
    │  (SQLite 元数据 + 文件系统)             │
    │  + URL 解析层（提前缓存检测）           │
    └────────────────────────────────────────┘
```

---

## 多平台下载器

项目使用工厂模式动态匹配下载器，支持以下平台：

| 平台 | 下载器类 | 特殊能力 |
|------|---------|---------|
| **YouTube** | YoutubeDownloader | 原生字幕、远程 API 服务器、yt-dlp 下载、实例级缓存优化 |
| **Bilibili** | BilibiliDownloader | TikHub API、BBDown 工具支持 |
| **抖音** | DouyinDownloader | TikHub API 获取无水印流 |
| **小红书** | XiaohongshuDownloader | TikHub v3 接口 |
| **小宇宙播客** | XiaoyuzhouDownloader | 网页爬虫解析 |
| **通用链接** | GenericDownloader | 直接流式下载、断点续传 |

**工厂模式实现**：

```python
def create_downloader(url):
    platform_downloaders = [
        DouyinDownloader(), BilibiliDownloader(),
        XiaohongshuDownloader(), YoutubeDownloader(),
        XiaoyuzhouDownloader()
    ]
    for downloader in platform_downloaders:
        if downloader.can_handle(url):
            return downloader
    return GenericDownloader()
```

**实例生命周期**：每个转录任务创建独立的下载器实例，任务结束后自动销毁，无内存泄漏和并发问题。YouTube 下载器支持实例级缓存，避免任务内的重复 API 请求。

---

## 双引擎转录

### CapsWriter-Offline（通用转录）

- WebSocket 实时流式传输
- 音频分段处理：25s 片段 + 2s 重叠
- 自动格式转换（MP3/WAV/M4A 等）
- 默认端口：6006

### FunASR（说话人识别）

- 支持分片上传（1MB/片）+ MD5 校验
- 说话人分离（Diarization）
- 队列状态查询
- 默认端口：8767

---

## LLM 智能文本处理

采用 **协调器-处理器-核心组件** 三层架构：

```
src/video_transcript_api/llm/
├── coordinator.py              # 统一入口 + 场景路由
├── core/                       # 核心基础组件（共享）
│   ├── config.py               # LLMConfig 统一配置类
│   ├── llm_client.py           # LLM 客户端（含智能重试）
│   ├── key_info_extractor.py   # 关键信息提取器
│   ├── speaker_inferencer.py   # 说话人推断器
│   ├── quality_validator.py    # 质量验证器
│   ├── cache_manager.py        # 缓存管理器
│   └── errors.py               # 错误分类模块
├── processors/                 # 独立的处理器
│   ├── plain_text_processor.py    # 无说话人文本处理器
│   ├── speaker_aware_processor.py # 有说话人文本处理器
│   └── summary_processor.py      # 总结处理器
├── segmenters/                 # 分段器
│   ├── text_segmenter.py         # 无说话人文本分段器
│   └── dialog_segmenter.py       # 有说话人文本分段器
├── prompts/                    # 提示词模板
│   └── schemas/                # JSON Schema 定义
└── llm.py                      # LLM API 基础调用
```

### 处理流程

```
LLMCoordinator.process(content, title, ...)
    │
    ├── 步骤 1: 模型选择（风险降级）
    │   └── config.select_models_for_task(has_risk)
    │
    ├── 步骤 2: 校对处理（场景路由）
    │   ├── str  → PlainTextProcessor.process()
    │   └── list → SpeakerAwareProcessor.process()
    │
    ├── 步骤 3: 总结生成（基于校对文本）
    │   └── SummaryProcessor.process()
    │
    └── 步骤 4: 合并结果
        └── {calibrated_text, summary_text, key_info, stats}
```

### 校对流程（4 步）

1. **提取关键信息** — 从视频元数据提取人名、术语、品牌（KeyInfoExtractor）
2. **说话人推断**（仅对话流）— 结合上下文推断真实姓名（SpeakerInferencer）
3. **智能分段 + 分段校对** — 并发处理，质量验证（TextSegmenter / DialogSegmenter）
4. **质量验证** — 长度检查 + 可选 LLM 打分（QualityValidator）

### 核心组件

| 组件 | 类名 | 主要职责 |
|------|------|---------|
| 统一配置 | `LLMConfig` | 集中管理所有 LLM 配置，支持风险模型切换 |
| 可靠调用 | `LLMClient` | 智能重试（错误分类 + 指数退避：5s → 10s → 20s → 40s → 60s） |
| 信息辅助 | `KeyInfoExtractor` | 从视频元数据提取关键信息，作为 Prompt 上下文 |
| 角色还原 | `SpeakerInferencer` | 将 `spk_0` 映射为真实姓名 |
| 质量防线 | `QualityValidator` | LLM 打分或长度比例验证 |
| 总结生成 | `SummaryProcessor` | 基于校对文本生成总结，支持单/多说话人模式 |

### 配置参数

**分段处理**：
- 触发阈值：`enable_threshold`（默认 5000 字符）
- 每段大小：`segment_size`（默认 2000 字符）
- 并发数：`concurrent_workers`（默认 10）

**智能重试**（LLMClient）：
- 自动区分可重试错误（超时、服务器错误）和不可重试错误（认证失败）
- 指数退避，最大 60s，最多 3 次重试

**质量阈值**：
- 整体评分：`overall_score`（默认 8.0）
- 单项评分：`minimum_single_score`（默认 7.0）

---

## 缓存系统

### 双层存储架构

- **SQLite 数据库**（`data/cache/cache.db`）：
  - `video_cache` 表：联合主键 `(platform, media_id, use_speaker_recognition)`
  - `task_status` 表：任务状态追踪（queued → processing → success/failed）
- **文件系统**：存储实际内容（转录文本、LLM 校对/总结、结构化 JSON）

### 目录结构

```
data/cache/
└── {platform}/
    └── {YYYY}/
        └── {YYYYMM}/
            └── {media_id}/
                ├── transcript_funasr.json
                ├── transcript_capswriter.txt
                ├── llm_calibrated.txt
                ├── llm_summary.txt
                ├── llm_processed.json
                ├── key_info.json
                └── speaker_mapping.json
```

### 智能缓存策略

- 请求带说话人识别时，仅匹配对应缓存
- 请求不带时，优先返回信息更丰富的说话人转录结果
- 完整性验证：文件夹不存在时自动清理数据库记录
- URL 解析优化：下载前提前检查缓存，支持短链接自动解析

---

## 企业级功能

### 多用户管理

- Bearer Token 认证
- 用户启用/禁用控制
- API Key 脱敏显示
- 配置文件：`config/users.json`

### 审计日志

- 记录 API 端点、请求/响应时间、处理耗时、状态码、用户信息
- 查询接口：`GET /api/audit/stats`、`GET /api/audit/calls`

### 企业微信通知

- 基于 `wecom-notifier` 库，全局单例频率控制（20 条/分钟）
- 超长文本自动分段、URL 保护模式
- 通知时机：任务创建、开始处理、缓存命中、任务完成

### 风控系统

- 远程动态加载敏感词库
- 多策略脱敏：`summary`（整体替换）、`title`（前 6 字符）、`general`（全移除）
- 风险模型自动切换：`risk_calibrate_model`、`risk_summary_model`

---

## Web 界面

### 任务提交

访问 `GET /add_task_by_web`，图形化提交转录任务。

### 结果查看

访问 `GET /view/{view_token}`，根据任务状态展示不同页面：

| 状态 | 模板 |
|------|------|
| `processing` | `processing.html` |
| `success` | `transcript.html`（含总结、校对文本、浮动目录） |
| `failed` | `error.html` |
| `file_cleaned` | `cleaned.html` |

### 导出模式

| 模式 | 地址 | 返回格式 | 适用场景 |
|------|------|---------|---------|
| Raw | `?raw=calibrated` | 纯文本 | 程序抓取、复制到 AI 平台 |
| Page | `?page=calibrated` | HTML 页面（含 meta 标签） | 爬虫抓取、浏览器阅读 |
| File | `/export/{token}/{type}` | 文件下载 | 离线使用 |

---

## 请求参数

### `POST /api/transcribe`

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `url` | string | 是 | 平台链接 |
| `use_speaker_recognition` | boolean | 否 | 是否启用说话人识别（默认 false） |
| `wechat_webhook` | string | 否 | 企业微信 webhook 地址 |
| `download_url` | string | 否 | 实际下载地址（跳过平台下载器） |
| `metadata_override` | object | 否 | 元数据覆盖（title/description/author） |

详见 [Download URL 与 Metadata Override 功能文档](features/source_url_and_metadata_override.md)。

---

## 添加新平台

1. 在 `src/video_transcript_api/downloaders/` 创建下载器类
2. 继承 `BaseDownloader`，实现 `can_handle()`、`get_video_info()`、`download_file()`
3. 在 `factory.py` 中注册
4. 添加测试用例
