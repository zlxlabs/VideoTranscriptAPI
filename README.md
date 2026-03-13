# 视频转录 API (Video Transcript API)

> 基于 Python 3.11+ 的异步视频转录服务，支持多平台下载、双引擎转录、智能文本处理和企业级功能集成。

**最新更新**：
- ✅ **架构重构完成**：LLM 引擎采用全新的模块化架构（协调器-处理器-核心组件三层设计）
- ✅ **URL 解析优化**：新增 `URLParser` 统一 URL 解析，支持短链接自动解析
- ✅ **智能重试机制**：LLM 调用支持错误分类和指数退避重试
- ✅ **总结功能恢复**：基于校对后文本生成高质量总结，支持单/多说话人模式
- ✅ **模块拆分**：utils 子模块按领域拆分（logging/, cache/, notifications/, accounts/ 等）

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

开发契机和玩法分享：[LLM 吞噬一切，我用 AI 长出来的那些工具](https://mp.weixin.qq.com/s/w8VnWJcUp5VkD5J-fYCUrg)

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
| **YouTube** | YoutubeDownloader | 原生字幕、远程 API 服务器、yt-dlp 下载、**实例级缓存优化** |
| **Bilibili** | BilibiliDownloader | TikHub API、BBDown 工具支持 |
| **抖音** | DouyinDownloader | TikHub API 获取无水印流 |
| **小红书** | XiaohongshuDownloader | TikHub v3 接口 |
| **小宇宙播客** | XiaoyuzhouDownloader | 网页爬虫解析 |
| **通用链接** | GenericDownloader | 直接流式下载、断点续传 |

**工厂模式实现**：
```python
def create_downloader(url):
    # 依次尝试平台特定下载器（每次调用创建新实例）
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

**实例生命周期**：
- 每个转录任务创建独立的下载器实例
- 实例级缓存避免任务内的重复 API 请求（例如 YouTube 下载器的 TikHub API 响应缓存）
- 任务结束后实例自动销毁，无内存泄漏风险
- 无并发问题（每个任务有独立实例）

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

**核心功能**（基于重构后的模块化架构）：

新架构采用 **"协调器-处理器-核心组件"** 的三层设计，实现了高度模块化和可扩展性。

**架构概览**：
```
src/video_transcript_api/llm/
├── coordinator.py              # 统一入口 + 场景路由
├── core/                       # 核心基础组件（共享）
│   ├── config.py               # LLMConfig 统一配置类
│   ├── llm_client.py           # LLM 客户端（含智能重试）
│   ├── key_info_extractor.py  # 关键信息提取器
│   ├── speaker_inferencer.py  # 说话人推断器
│   ├── quality_validator.py   # 质量验证器
│   ├── cache_manager.py       # 缓存管理器
│   └── errors.py              # 错误分类模块
├── processors/                 # 独立的处理器
│   ├── plain_text_processor.py   # 无说话人文本处理器
│   ├── speaker_aware_processor.py # 有说话人文本处理器
│   └── summary_processor.py     # 内容总结处理器
├── segmenters/                 # 分段器
│   ├── text_segmenter.py        # 无说话人文本分段器
│   └── dialog_segmenter.py      # 有说话人文本分段器
├── prompts/                    # 提示词模板
│   └── schemas/                # JSON Schema 定义
└── llm.py                      # LLM API 基础调用
```

#### 新架构概览

```
┌─────────────────────────────────────────────────────────────┐
│                   LLMCoordinator                        │
│                  (统一入口 + 场景路由)                     │
├─────────────────────────────────────────────────────────────┤
│                                                         │
│  统一入口: process(content, title, ...) → Dict          │
│                                                         │
│  ┌────────────────────────────────────────────────┐    │
│  │  步骤 1: 模型选择（风险降级）                    │    │
│  │                                                 │    │
│  │  selected_models = config.select_models_for_task(  │    │
│  │      has_risk  # 根据 enable_risk_model_selection    │    │
│  │  )                                               │    │
│  └────────────────────────────────────────────────┘    │
│                          ↓                              │
│  ┌────────────────────────────────────────────────┐    │
│  │  步骤 2: 校对处理（场景路由）                   │    │
│  │                                                 │    │
│  │  if isinstance(content, str):                   │    │
│  │      → PlainTextProcessor.process()             │    │
│  │  elif isinstance(content, list):                │    │
│  │      → SpeakerAwareProcessor.process()          │    │
│  │                                                 │    │
│  │  返回：                                          │    │
│  │  {                                              │    │
│  │      "calibrated_text": str,                    │    │
│  │      "key_info": dict,                          │    │
│  │      "structured_data": dict (可选)             │    │
│  │  }                                              │    │
│  └────────────────────────────────────────────────┘    │
│                          ↓                              │
│  ┌────────────────────────────────────────────────┐    │
│  │  步骤 3: 总结生成（基于校对文本）             │    │
│  │                                                 │    │
│  │  if len(calibrated_text) >= min_threshold:      │    │
│  │      → SummaryProcessor.process()               │    │
│  │          输入：calibrated_text                   │    │
│  │          返回：summary_text                      │    │
│  │  else:                                          │    │
│  │      summary_text = None                        │    │
│  └────────────────────────────────────────────────┘    │
│                          ↓                              │
│  ┌────────────────────────────────────────────────┐    │
│  │  步骤 4: 合并结果                               │    │
│  │                                                 │    │
│  │  return {                                       │    │
│  │      "calibrated_text": str,                    │    │
│  │      "summary_text": Optional[str],              │    │
│  │      "key_info": dict,                          │    │
│  │      "stats": dict,                             │    │
│  │      "structured_data": dict (可选)             │    │
│  │  }                                              │    │
│  └────────────────────────────────────────────────┘    │
└─────────────────────────────────────────────────────────────┘
```

#### 核心功能

1. **自动校对（Calibration）**
   - 修正语音识别中的同音字和语法错误
   - 标点符号规范化
   - 支持长文本自动分段并发处理
   - 配置模型：`calibrate_model`

   **校对流程（4 步）**：
   - **步骤 1：提取关键信息** - 从视频元数据提取人名、术语、品牌等（KeyInfoExtractor）
   - **步骤 1.5：说话人推断**（仅对话流）- 结合上下文推断真实姓名（SpeakerInferencer）
   - **步骤 2：智能分段** - 根据文本类型选择分段策略（TextSegmenter / DialogSegmenter）
   - **步骤 3：分段校对** - 并发处理每个分段，质量验证（PlainTextProcessor / SpeakerAwareProcessor）
   - **步骤 4：质量验证** - 长度检查 + 可选的 LLM 打分（QualityValidator）

2. **内容总结（Summary）**
   - **基于校对后的文本生成总结**（质量更高）
   - 支持单说话人和多说话人模式（自动选择 Prompt）
   - 配置模型：`summary_model`
   - 最小文本阈值：`min_summary_threshold`（默认 500 字符）
   - **串行执行**：先校对，再总结（约增加 15-20 秒处理时间）
   - **新增**：`SummaryProcessor` 独立处理器，模块化设计

   **总结流程**：
   - 长度检查（< 500 字符则跳过）
   - 选择 Prompt（单说话人 / 多说话人）
   - 调用 LLM 生成总结（含 task_type 参数用于追踪）
   - 结果验证（> 50 字符）

3. **说话人推断（Speaker Inference）**
   - 结合视频元数据推断真实姓名
   - 将匿名标识（`spk_0`, `spk_1`）映射为具体人名
   - 支持对话结构保留
   - 基于前 1000 字符对话样本推断
   - 结果缓存（按 platform + media_id）

4. **风险模型切换**
   - 自动检测敏感内容
   - 切换到专用风险模型：`risk_calibrate_model`、`risk_summary_model`、`risk_validator_model`
   - 配置开关：`enable_risk_model_selection`
   - 统一配置类 `LLMConfig` 管理所有模型选择
   - **新增**：`select_models_for_task()` 方法，根据 `has_risk` 参数动态选择模型

#### 核心组件

| 组件 | 类名 | 主要职责 |
|------|------|---------|
| **统一配置** | `LLMConfig` | 集中管理所有 LLM 相关配置（模型、阈值、并发数等），支持风险模型切换 |
| **可靠调用** | `LLMClient` | 封装底层 API，实现**智能重试**（错误分类 + 指数退避：5s → 10s → 20s → 40s → 60s） |
| **错误分类** | `RetryableError`, `FatalError` | 自动识别可重试错误（超时、服务器错误）和不可重试错误（认证失败、配置错误） |
| **信息辅助** | `KeyInfoExtractor` | 从视频元数据提取人名、术语、品牌等，作为 Prompt 的上下文，支持缓存复用 |
| **角色还原** | `SpeakerInferencer` | 将 `spk_0` 等匿名标识映射为真实姓名，基于前 1000 字符对话样本推断 |
| **质量防线** | `QualityValidator` | 通过 LLM 打分（准确性、流畅度、格式）或长度比例验证校对结果 |
| **状态持久化** | `CacheManager` | 缓存 `KeyInfo` 和 `SpeakerMapping` 到视频目录（与视频缓存同目录），避免重复调用 |
| **总结生成** | `SummaryProcessor` | 基于校对后的文本生成内容总结，支持单说话人和多说话人模式 |

#### 处理器

| 处理器 | 输入类型 | 核心流程 |
|--------|---------|---------|
| **PlainTextProcessor** | `str` (纯文本) | 提取关键信息 → 按句子/行分段 → 分段校对（并发）→ 质量验证 |
| **SpeakerAwareProcessor** | `List[Dict]` (对话流) | 提取关键信息 → 说话人推断 → 按对话长度分段 → 结构化校对（并发）→ 质量验证 |
| **SummaryProcessor** | `str` (校对文本) | 长度检查 → 选择 Prompt（单说话人/多说话人）→ 调用 LLM → 验证结果（>50 字符）|

#### 分段器

| 分段器 | 适用场景 | 分段策略 |
|--------|---------|---------|
| **TextSegmenter** | 无说话人文本 | 标点密度检测（< 5/1000 按行，否则按句子） |
| **DialogSegmenter** | 有说话人对话 | 按对话长度分段（保持对话完整性） |

#### 配置参数

**分段处理策略**：
- 触发阈值：`enable_threshold`（默认 5000 字符）
- 每段大小：`segment_size`（默认 2000 字符）
- 最大段大小：`max_segment_size`（默认 3000 字符）
- 并发数：`concurrent_workers`（默认 10）

**结构化校对**（带说话人识别时）：
- 单块最小长度：`min_chunk_length`（默认 300）
- 单块最大长度：`max_chunk_length`（默认 1500）
- 首选块长度：`preferred_chunk_length`（默认 800）
- 质量验证：`enable_validation`（默认 false）

**JSON 输出模式**：
- 按模型名匹配输出模式：`mode_by_model`
- 支持 `json_object` 和 `json_schema` 两种模式
- 自动重试：`max_retries`（默认 2 次）

**智能重试**（LLMClient）：
- 错误分类：自动识别 `FatalError`（不可重试：认证失败、配置错误）和 `RetryableError`（可重试：超时、服务器错误、速率限制）
- 指数退避：5s → 10s → 20s → 40s → 60s（最大 60s）
- 最大重试次数：`max_retries`（默认 3 次）
- **新增**：快速失败机制，避免无效重试

**质量阈值**：
- 整体评分阈值：`overall_score`（默认 8.0）
- 单项评分阈值：`minimum_single_score`（默认 7.0）

### 🏗️ 企业级功能

#### 智能缓存系统

**数据存储结构**：

**URL 解析优化**（新增）：
- 使用 `URLParser` 统一解析平台和 video_id
- 提前缓存检测：在下载前检查缓存，避免不必要的 API 请求
- 支持短链接自动解析（HTTP HEAD 请求）
- 无法识别的 URL 自动回退到 `generic` 平台

**双层存储架构**：
- **SQLite 数据库**（`data/cache/cache.db`）：
  - `video_cache` 表：平台、URL、标题、作者、媒体 ID、说话人标识、文件位置、LLM 配置参数
    - 联合主键：`platform`, `media_id`, `use_speaker_recognition`
    - 索引：`idx_platform_media_id`（查询优化）、`idx_url`（URL 匹配）
  - `task_status` 表：任务 ID、查看令牌、状态、创建/完成时间
    - 状态类型：`queued`, `processing`, `success`, `failed`
    - LLM 配置追踪：`llm_config` 字段（JSON）存储处理时的模型参数
- **文件系统**：存储实际内容（转录文本、LLM 校对、LLM 总结、结构化 JSON）

**目录结构**（按时间分层）：
```
data/cache/
└── {platform}/
    └── {YYYY}/
        └── {YYYYMM}/
            └── {media_id}/
                ├── transcript_funasr.json       # FunASR 转录（含时间戳、说话人）
                ├── transcript_capswriter.txt    # CapsWriter 转录
                ├── llm_calibrated.txt          # LLM 校对后的文本
                ├── llm_summary.txt             # LLM 生成的总结
                ├── llm_processed.json          # 结构化处理结果（V2 格式）
                ├── key_info.json              # 关键信息缓存（新增）
                └── speaker_mapping.json       # 说话人映射缓存（新增）
```

**查询逻辑**：
```python
cache_data = cache_manager.get_cache(
    platform=platform,
    media_id=video_id,
    use_speaker_recognition=use_speaker_recognition
)
```

**智能缓存策略**（优先级逻辑）：
- **请求 `use_speaker_recognition=True`**：仅匹配带说话人识别的缓存
- **请求 `use_speaker_recognition=False`**：查询所有记录，但通过 `ORDER BY use_speaker_recognition DESC` 排序
- **智能回退**：如果系统中同时存在同一个视频的"普通转录"和"带说话人识别转录"，系统会**优先返回信息更丰富的说话人转录结果**，即使用户并未明确要求
- **完整性验证**：查询时若发现物理文件夹不存在，会立即 `DELETE` 数据库记录并返回 `None`

**关键特性**：
- **线程安全**：使用 `threading.local()` 为每个线程维护独立的 SQLite 连接
- **增量保存**：`save_llm_result` 允许在任务完成后，增量地向同一个缓存目录追加校对文本、总结或结构化 JSON
- **元数据追踪**：`llm_config` 字段记录生成结果时使用的模型参数，便于后续分析

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
    │  工厂   │    │ 双引擎   │    │  协调器  │
    │+URL解析│    │         │    │ (新架构） │
    └────┬────┘    └────┬────┘    └────┬──────┘
         │              │               │
         │              │               │
    ┌────▼──────────────▼──────────────▼────┐
    │         智能缓存系统                │
    │  (SQLite 元数据 + 文件系统)         │
    │  + URL 解析层（提前缓存检测）        │
    └─────────────────────────────────────┘
```

### 核心模块

| 模块 | 文件路径 | 职责 |
|------|---------|------|
| **API 服务** | `api/` | FastAPI 应用、路由、依赖注入 |
| **下载器** | `downloaders/` | 多平台内容获取、工厂模式、实例级缓存 |
| **转录器** | `transcriber/` | 语音识别、说话人识别 |
| **LLM 引擎** | `llm/` | 文本校对、总结、分段、结构化校对（已重构为模块化架构） |
| **缓存系统** | `cache/` | 元数据存储、文件管理 |
| **通知系统** | `utils/notifications/` | 企业微信消息推送（WeComNotifier） |
| **风控模块** | `utils/risk_control/` | 敏感词检测、内容脱敏 |
| **用户管理** | `utils/accounts/` | 多用户鉴权、配置管理 |
| **审计日志** | `utils/logging/` | API 调用追踪、统计 |
| **工具模块** | `utils/` | URL 解析、临时文件管理、日志配置等工具函数 |

---

## 架构优化亮点

### V2.0 架构重构（2026-01-27）

本次更新对 LLM 引擎和业务流程进行了全面重构，主要优化包括：

#### 1. LLM 模块化架构

**重构前**：
- 功能混杂在单个文件中
- 代码耦合严重，难以维护
- 新增功能需要修改多个地方

**重构后**：
- **三层设计**：协调器 → 处理器 → 核心组件
- **职责清晰**：每个模块单一职责，易于测试和扩展
- **共享基础组件**：KeyInfoExtractor、SpeakerInferencer、QualityValidator 等可复用

#### 2. 智能重试机制

**特性**：
- 自动错误分类：区分可重试错误（超时、服务器错误）和不可重试错误（认证失败、配置错误）
- 指数退避：5s → 10s → 20s → 40s → 60s（最大 60s）
- 快速失败：致命错误立即返回，避免浪费时间

**效果**：
- 提高系统健壮性，降低网络波动影响
- 减少 50% 的无效等待时间

#### 3. URL 解析优化

**新增模块**：`utils/url_parser.py`

**功能**：
- 统一解析 5 大平台：YouTube、Bilibili、抖音、小红书、小宇宙
- 支持短链接自动解析（HTTP HEAD 请求）
- 提前缓存检测：在下载前检查缓存，避免不必要的 API 请求

**效果**：
- 缓存命中率提升
- 减少外部 API 调用（节省成本）

#### 4. 下载器实例级缓存

**优化**：
- YouTube 下载器：`get_video_info()` 和 `_get_subtitle_with_tikhub_api()` 复用同一次 API 响应
- 实例生命周期：与单个转录任务绑定，任务结束后自动释放

**效果**：
- 减少 50% 的 TikHub API 请求（针对 YouTube 视频）
- 无并发问题，无内存泄漏风险

#### 5. 总结功能恢复

**新特性**：
- 独立 `SummaryProcessor` 处理器
- 基于校对后的文本生成总结（质量更高）
- 支持单说话人和多说话人模式
- 串行执行：先校对，再总结

**效果**：
- 总结质量显著提升
- 与校对内容完全一致

#### 6. 模块拆分

**重构**：utils 子模块按领域拆分

```
utils/
├── logging/           # 日志系统
├── cache/            # 缓存系统（从 llm/cache_manager 移出）
├── notifications/     # 通知系统
├── accounts/         # 用户管理
├── risk_control/      # 风控模块
├── rendering/         # 渲染工具
├── timeutil/          # 时间工具
├── url_parser.py      # URL 解析（新增）
└── tempfile_manager.py # 临时文件管理
```

**效果**：
- 代码组织更清晰
- 依赖关系更合理
- 易于单独测试和维护

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

### 🌐 Page 模式导出（HTML 页面）

**访问地址**：`GET /view/{view_token}?page=calibrated`

**支持的导出类型**：`calibrated`、`summary`、`transcript`

**功能说明**：
- 返回完整的 HTML 页面，包含正确的 `<title>`、Open Graph meta 标签
- 正文经过 Markdown → HTML 渲染，排版清晰
- 极简语义化页面，适合爬虫抓取和浏览器阅读
- 同时附带 `X-Document-Title`、`X-Platform` 等自定义 HTTP 响应头

**响应示例**：
```http
HTTP/1.1 200 OK
Content-Type: text/html; charset=utf-8
X-Document-Title: 视频标题
X-Platform: youtube

<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <title>视频标题 - 校对文本</title>
    <meta property="og:title" content="视频标题 - 校对文本">
    ...
</head>
<body>
    <article>（渲染后的内容）</article>
</body>
</html>
```

**与 Raw 模式对比**：

| | `?raw=` | `?page=` |
|---|---|---|
| 返回格式 | 纯文本 + YAML front matter | 完整 HTML 页面 |
| 浏览器标题 | 显示 URL | 显示「视频标题 - 校对文本」|
| Meta 标签 | 无（纯文本） | Open Graph、description 等 |
| 正文渲染 | 原始文本 | Markdown → HTML |
| 适用场景 | 程序抓取原始内容 | 爬虫抓取、浏览器阅读 |

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
| `/view/{view_token}?page=calibrated` | GET | Page 模式（HTML 页面导出，爬虫友好） |
| `/export/{view_token}/{type}` | GET | 导出处理结果 |

### 请求参数

**`POST /api/transcribe`**：
```json
{
  "url": "视频URL（必填，平台链接）",
  "use_speaker_recognition": "是否使用说话人识别（默认 false）",
  "wechat_webhook": "企业微信 webhook 地址（可选）",
  "download_url": "实际下载地址（可选，如果提供则优先使用）",
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
| `url` | string | 是 | **平台链接**（用于 view_token 生成、缓存查询、元数据解析） |
| `use_speaker_recognition` | boolean | 否 | 是否使用说话人识别功能（默认 false） |
| `wechat_webhook` | string | 否 | 企业微信 webhook 地址，用于发送通知 |
| `download_url` | string | 否 | **实际下载地址**（可选，如果提供则优先使用，用于本地文件场景） |
| `metadata_override` | object | 否 | 元数据覆盖对象，用于补充或覆盖解析的元数据 |
| `metadata_override.title` | string | 否 | 视频标题 |
| `metadata_override.description` | string | 否 | 视频描述 |
| `metadata_override.author` | string | 否 | 视频作者 |

#### 注意事项

- view_token 基于 `url`（平台链接）生成，`download_url` 仅影响实际下载地址。
- 提供 `download_url` 时会跳过平台字幕/平台下载器/YouTube API Server，强制使用 `download_url` 下载。

#### 使用场景示例

**场景 1：本地文件 + 原始元数据保留**

当你在本地下载了视频文件（如 YouTube 视频），并通过本地 HTTP 服务器暴露文件时，可以使用 `download_url` 指定实际下载地址：

```json
{
  "url": "https://www.youtube.com/watch?v=abc123",
  "download_url": "http://localhost:8080/video.mp4",
  "use_speaker_recognition": true
}
```

**场景 2：手动补充元数据**

当自动解析的元数据不准确时，可以使用 `metadata_override` 手动提供：

```json
{
  "url": "https://www.youtube.com/watch?v=abc123",
  "download_url": "http://localhost:8080/video.mp4",
  "metadata_override": {
    "title": "更准确的中文标题",
    "description": "补充的详细描述"
  }
}
```

**详细说明**：参见 [Download URL 和 Metadata Override 功能文档](docs/features/source_url_and_metadata_override.md)

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

### 🌐 Page 模式导出（HTML 页面）

**访问地址**：`GET /view/{view_token}?page=calibrated`

**支持的导出类型**：`calibrated`、`summary`、`transcript`

**功能说明**：
- 返回完整 HTML 页面，包含 `<title>`、Open Graph 等 meta 标签
- 正文经过 Markdown → HTML 渲染
- 极简语义化页面，适合爬虫抓取和浏览器阅读

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

- [企业微信通知](docs/guides/wechat_notification.md) - WeComNotifier 使用指南
- [多用户系统](docs/guides/multi_user_setup.md) - 用户管理、权限控制
- [API 使用指南](docs/guides/api/) - 各平台 API 详细说明
- [文档索引](docs/README.md) - 完整的文档导航

### 🔧 开发文档

- [LLM 工程指南](docs/development/llm/engineering_guide.md) - Prompt 优化、结构化输出
- [LLM 重构方案](docs/development/llm/refactoring_plan.md) - 模块化架构设计方案
- [LLM 重构完成报告](docs/development/llm/refactoring_completed.md) - 重构实施总结
- [LLM 总结功能设计](docs/development/llm/summary_feature_design.md) - 总结功能恢复设计
- [模块迁移快速开始](docs/development/module_migration_quickstart.md) - 模块迁移指南
- [模块迁移计划](docs/development/module_migration_plan.md) - 模块拆分方案
- [模块迁移总结](docs/development/module_migration_summary.md) - 模块迁移完成报告
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
│   └── video_transcript_api/
│       ├── api/                    # FastAPI 服务
│       │   ├── routes/             # API 路由（tasks, users, audit, views）
│       │   ├── services/           # 业务逻辑（transcription）
│       │   └── context.py          # 请求上下文
│       ├── downloaders/            # 平台下载器
│       │   ├── base.py             # 下载器基类
│       │   ├── factory.py          # 下载器工厂
│       │   ├── models.py           # 数据模型
│       │   ├── youtube.py          # YouTube 下载器（含实例级缓存）
│       │   ├── bilibili.py         # Bilibili 下载器
│       │   ├── douyin.py           # 抖音下载器
│       │   ├── xiaohongshu.py      # 小红书下载器
│       │   └── xiaoyuzhou.py       # 小宇宙播客下载器
│       ├── transcriber/            # 转录引擎
│       │   ├── transcriber.py      # 转录器入口
│       │   ├── funasr_client.py    # FunASR 客户端
│       │   └── capswriter_client.py # CapsWriter 客户端
│       ├── llm/                    # LLM 处理引擎（重构后）
│       │   ├── coordinator.py       # LLM 协调器（统一入口）
│       │   ├── core/               # 核心基础组件
│       │   │   ├── config.py       # LLMConfig 统一配置
│       │   │   ├── llm_client.py   # LLM 客户端（智能重试）
│       │   │   ├── key_info_extractor.py  # 关键信息提取器
│       │   │   ├── speaker_inferencer.py  # 说话人推断器
│       │   │   ├── quality_validator.py   # 质量验证器
│       │   │   ├── cache_manager.py       # 缓存管理器
│       │   │   └── errors.py      # 错误分类
│       │   ├── processors/         # 独立的处理器
│       │   │   ├── plain_text_processor.py   # 无说话人文本处理器
│       │   │   ├── speaker_aware_processor.py # 有说话人文本处理器
│       │   │   └── summary_processor.py     # 总结处理器
│       │   ├── segmenters/         # 分段器
│       │   │   ├── text_segmenter.py        # 无说话人文本分段器
│       │   │   └── dialog_segmenter.py      # 有说话人文本分段器
│       │   ├── prompts/            # 提示词模板
│       │   │   └── schemas/       # JSON Schema 定义
│       │   └── llm.py              # LLM API 基础调用
│       ├── cache/                  # 缓存系统
│       │   ├── cache_manager.py    # 缓存管理器（SQLite + 文件系统）
│       │   └── cache_analyzer.py   # 缓存分析工具
│       ├── utils/                  # 工具模块（按领域拆分）
│       │   ├── logging/            # 日志系统
│       │   │   ├── logger.py       # Loguru 配置
│       │   │   └── audit_logger.py # 审计日志
│       │   ├── notifications/      # 通知系统
│       │   │   └── wechat.py      # 企业微信通知
│       │   ├── accounts/           # 用户管理
│       │   │   └── user_manager.py
│       │   ├── risk_control/       # 风控模块
│       │   │   ├── text_sanitizer.py       # 文本脱敏
│       │   │   └── sensitive_words_manager.py # 敏感词管理
│       │   ├── rendering/          # 渲染工具
│       │   │   ├── dialog_renderer.py    # 对话渲染
│       │   │   └── markdown_renderer.py  # Markdown 渲染
│       │   ├── timeutil/           # 时间工具
│       │   │   └── timezone_helper.py
│       │   ├── url_parser.py       # URL 解析器（新增）
│       │   ├── tempfile_manager.py # 临时文件管理
│       │   └── __init__.py
│       └── __init__.py
├── tests/                         # 测试套件
│   ├── unit/                       # 单元测试
│   ├── integration/                 # 集成测试
│   ├── llm/                        # LLM 功能测试
│   ├── cache/                      # 缓存功能测试
│   ├── features/                   # 核心功能测试
│   └── platforms/                  # 平台适配测试
├── docs/                          # 文档中心
│   ├── guides/                     # 使用指南
│   ├── development/                # 开发文档
│   │   ├── llm/                   # LLM 开发指南
│   │   ├── platforms/             # 平台适配指南
│   │   ├── module_migration_*      # 模块迁移文档
│   │   └── architecture_optimization_* # 架构优化文档
│   └── features/                   # 功能特性
├── config/                        # 配置文件
│   ├── config.example.jsonc         # 配置模板
│   └── users.json                  # 用户配置
├── scripts/                       # 工具脚本
└── main.py                        # 入口文件
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
