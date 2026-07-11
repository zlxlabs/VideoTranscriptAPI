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
| **Apple Podcast** | ApplePodcastDownloader | 网页解析获取音频直链 |
| **通用链接** | GenericDownloader | 直接流式下载、断点续传（SSRF 校验 + 重定向逐跳校验） |

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
│   ├── llm_client.py           # LLM 客户端薄封装（重试/翻译/降级由 llm-compat 处理）
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
└── llm.py                      # LLM API 调用（通过 llm-compat SyncLLMClient）
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
2. **说话人推断**（仅对话流）— 按说话人采样发言样本 + 首次出场上下文，结合关键信息推断真实姓名，置信度低于阈值时降级为"说话人N"占位符（SpeakerInferencer，详见下方"说话人推断"）
3. **智能分段 + 分段校对** — 并发处理，质量验证（TextSegmenter / DialogSegmenter）
4. **质量验证** — 长度检查 + 可选 LLM 打分（QualityValidator）

### 说话人推断

`SpeakerInferencer`（`llm/core/speaker_inferencer.py`）按说话人采样而非全局前 N 字符截断，确保晚出场的说话人也能拿到足够样本：

- 每个说话人取前 `samples_per_speaker` 条发言（默认 3 条，每条截断 120 字符），总字符数不超过 `max_chars_per_speaker`（默认 400）
- 首次出场前额外采集 `context_dialogs` 条他人发言作为上下文（默认 2 条，用于捕捉"XX你好"之类的称呼线索）
- LLM 推断结果携带 confidence；低于 `confidence_threshold`（默认 0.6）的映射不采用推断姓名，而是降级为"说话人N"占位符（N 优先取原始标签数字序号，否则按出场顺序编号），避免把低置信度的猜测当作确定结论展示给用户
- 四个参数均可在 `config.jsonc` 的 `llm.speaker_inference` 段配置，见下方"配置参数"

### 诚实状态模型

校对（`CalibrationStatus`）与总结（`SummaryStatus`）状态定义在 `utils/llm_status.py`，贯穿 processor → coordinator → llm_ops → cache_manager → 前端这条链路，取代早期"用 None 兼表示跳过和失败"导致的二义性（如"总结处理中..."永久占位符 bug）：

| 状态类 | 取值 | 含义 |
|---|---|---|
| `CalibrationStatus` | `full` | 全部内容成功由 LLM 校对，没有任何原文兜底 |
| | `partial` | 部分内容降级为原文或低质量输出 |
| | `none` | 全部内容降级为原文（LLM 校对完全失败） |
| | `disabled` | 用户通过 `processing_options.calibrate=false` 主动关闭校对（区别于 `none`：`none` 是"尝试了但失败"，`disabled` 是"根本没尝试"） |
| `SummaryStatus` | `generated` | 总结成功生成 |
| | `skipped_short` | 原文过短，未触发总结生成（正常路径，非失败） |
| | `failed` | 触发了生成但失败（LLM 异常或输出过短/为空） |
| | `pending` | 总结阶段尚未执行完成 |
| | `disabled` | 用户通过 `processing_options.summarize=false` 主动关闭总结 |

落盘载体：缓存目录下的 `llm_status.json`（读-改-写按字段合并，未传字段保留旧值）+ `task_status` 表的 `calibration_status`/`summary_status` 两列（作为 JSON 的镜像，供 `/api/audit/history` 查询消费而不必逐个打开缓存文件）。`/api/audit/summary` 据此只在 `summary_status == generated` 时返回真实文本，其余状态一律返回 `null`，不再用占位字符串掩盖失败。

处理深度开关（`processing_options`）与分层缓存复用的完整语义，见 [处理深度开关功能文档](features/processing_options.md)。

### 核心组件

| 组件 | 类名 | 主要职责 |
|------|------|---------|
| 统一配置 | `LLMConfig` | 集中管理所有 LLM 配置，支持风险模型切换 |
| 可靠调用 | `LLMClient` | 薄封装（重试/翻译/降级由 llm-compat SyncLLMClient 内部处理） |
| 信息辅助 | `KeyInfoExtractor` | 从视频元数据提取关键信息，作为 Prompt 上下文 |
| 角色还原 | `SpeakerInferencer` | 将 `spk_0` 映射为真实姓名 |
| 质量防线 | `QualityValidator` | LLM 打分或长度比例验证 |
| 总结生成 | `SummaryProcessor` | 基于校对文本生成总结，支持单/多说话人模式 |

### 配置参数

**分段处理**：
- 触发阈值：`enable_threshold`（默认 5000 字符）
- 每段大小：`segment_size`（默认 2000 字符）
- 并发数：`concurrent_workers`（默认 10）

**LLM 调用**（通过 llm-compat）：
- 重试、指数退避、provider 翻译由 llm-compat 内部处理
- 内容审查降级：主模型被拒时自动切换 fallback 模型（通过 `content_fallbacks` 配置）
- 可选 Collector 集成：跨项目敏感词积累

**质量阈值**：
- 整体评分：`overall_score`（默认 8.0）
- 单项评分：`minimum_single_score`（默认 7.0）

**说话人推断采样**（`llm.speaker_inference`）：
- 每人采样条数：`samples_per_speaker`（默认 3）
- 每人采样字符上限：`max_chars_per_speaker`（默认 400）
- 首次出场前上下文条数：`context_dialogs`（默认 2）
- 置信度阈值：`confidence_threshold`（默认 0.6，低于此值降级为"说话人N"）

---

## 缓存系统

### 双层存储架构

- **SQLite 数据库**（`data/cache/cache.db`）：
  - `video_cache` 表：联合主键 `(platform, media_id, use_speaker_recognition)`
  - `task_status` 表：任务状态追踪（queued → processing → success/failed），另有 `calibration_status`/`summary_status` 两列镜像诚实状态（见"诚实状态模型"），终态记录按 `storage.task_status_retention_days`（默认 180 天）周期清理
- **文件系统**：存储实际内容（转录文本、LLM 校对/总结、结构化 JSON、`llm_status.json` 诚实状态文件）

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
                ├── llm_status.json     # 诚实状态模型：calibration_status/summary_status
                ├── key_info.json
                └── speaker_mapping.json
```

### 智能缓存策略

- 请求带说话人识别时，仅匹配对应缓存
- 请求不带时，优先返回信息更丰富的说话人转录结果
- 完整性验证：文件夹不存在时自动清理数据库记录
- URL 解析优化：下载前提前检查缓存，支持短链接自动解析

---

## 临时文件清理

`data/temp` 存放转录的输入：下载的源视频与提取的音频中间件。这些文件转录段一结束即成废物，由 `TempFileManager`（`utils/tempfile_manager.py`）统一管理，避免长期堆积撑满磁盘。

### 任务专属目录

每个任务在 `data/temp/task_<task_id>/` 下落所有临时文件（含 yt-dlp / BBDown / youtube-api 的中间产物，统一重定向到此目录，不再泄漏到系统 `/tmp`）。任务目录天然隔离，不同任务即使同名文件也互不影响。

```
data/temp/
├── task_<id1>/          # 任务1：源视频 + 提取音频 + 下载器中间件
└── task_<id2>/          # 任务2：与任务1隔离
```

### 清理时机（双层）

- **治本——终态清理**：`process_transcription` 最外层 `try/finally` 中，任务结束（成功 / 失败 / 异常 / 缓存命中）后 `rmtree` 该任务目录。临时文件只是转录输入，不依赖 LLM 阶段终态。
- **治标——兜底扫描**：进程内的惰性扫描（任务开始时触发，按 `temp_retention_hours` 节流）+ 启动时清扫，清理崩溃 / 强杀残留的孤儿目录。无额外系统组件（无 cron / sidecar）。

### 在途保护

扫描删除的条件是「**不属于任何活跃任务** 且 **mtime 超过 `temp_retention_hours`**」双条件。活跃任务（如多小时的直播录像下载）由活跃登记表保护，不会被按 mtime 一刀切误删；优雅关闭时也只清理非活跃任务目录。

相关配置：`storage.temp_retention_hours`（默认 24 小时）。

---

## 企业级功能

### 多用户管理

- Bearer Token 认证
- 用户启用/禁用控制
- API Key 脱敏显示
- 配置文件：`config/users.json`

### 审计日志

- 记录 API 端点、请求/响应时间、处理耗时、状态码、用户信息
- **LLM token 用量审计**（`audit.db` schema v3，`llm_usage` 表）：每次 `LLMClient.call()` 调用记一行，含 `task_id`/`stage`（calibration/summary/speaker_inference/validation 等）/`model`/prompt·completion·total tokens/耗时；provider 未回报用量时仍写入一行并标记 `usage_missing`，避免静默丢弃
- 已知限制：标题生成（通用下载器场景）直接调用 `call_llm_api()`，未经过 `LLMClient.call()`，不计入用量统计；llm-compat 内部 Self-Correction 重试只有最后一次的 usage 被记录（桥接槽在同一线程内"写入→立即读出并清空"，中间重试的用量不可见）
- 查询接口：`GET /api/audit/stats`（含 `llm_usage` 按 stage 聚合 + 总计）、`GET /api/audit/calls`、`GET /api/audit/history`（含状态字段）

### 多渠道通知

- 基于 `wecom-notifier` 库（v0.3.1+），支持企业微信和飞书双平台
- `NotificationRouter` 路由层按配置自动分发到所有启用的渠道
- 支持 per-channel webhook：全局配置、用户级配置、per-request 指定
- 渠道 fallback：目标渠道失败时自动退到备用渠道
- 超长文本自动分段、URL 保护模式、频率控制（各渠道独立）
- 通知时机：任务创建、开始处理、缓存命中、转录完成、LLM 完成、ASR 告警

### 风控系统

- 远程动态加载敏感词库
- 多策略脱敏：`summary`（整体替换）、`title`（前 6 字符）、`general`（全移除）
- 风险模型自动切换：`risk_calibrate_model`、`risk_summary_model`

---

## Web 界面

三个页面（`add_task_by_web`、`transcript.html`、`history.html`）共享统一站内导航（`site-nav`），`history.html` 已适配移动端响应式布局。

### 任务提交

访问 `GET /add_task_by_web`，图形化提交转录任务。

### 结果查看

访问 `GET /view/{view_token}`，根据任务状态展示不同页面：

| 状态 | 模板 |
|------|------|
| `processing` | `processing.html` |
| `success` | `transcript.html`（含总结、校对文本、浮动目录、正文"复制内容"按钮） |
| `failed` | `error.html` |
| `file_cleaned` | `cleaned.html` |

`transcript.html` 按诚实状态模型渲染：校对区在 `calibration_status` 为 `partial`/`none` 时展示质量警告条，为 `disabled` 时展示"未启用 AI 校对"提示；总结区按 `summary_status` 展示四态文案（`pending`→处理中、`skipped_short`→文本过短未生成、`failed`→生成失败、`disabled`→未启用）。

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
| `wechat_webhook` | string | 否 | 企业微信 webhook 地址（向后兼容） |
| `notification_config` | object | 否 | 通知配置：`{channel: "feishu", webhook: "..."}` |
| `download_url` | string | 否 | 实际下载地址（跳过平台下载器） |
| `metadata_override` | object | 否 | 元数据覆盖（title/description/author） |
| `processing_options` | object | 否 | 处理深度开关：`{calibrate: bool=true, summarize: bool=true}`，`null` 等价于全部启用 |

详见 [Download URL 与 Metadata Override 功能文档](features/source_url_and_metadata_override.md)、[处理深度开关功能文档](features/processing_options.md)。

---

## 添加新平台

1. 在 `src/video_transcript_api/downloaders/` 创建下载器类
2. 继承 `BaseDownloader`，实现 `can_handle()`、`get_video_info()`、`download_file()`
3. 在 `factory.py` 中注册
4. 添加测试用例
