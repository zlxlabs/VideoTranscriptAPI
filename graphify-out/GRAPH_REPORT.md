# Graph Report - src + docs  (2026-04-13)

## Corpus Check
- 140 files · ~112,241 words
- Verdict: corpus is large enough that graph structure adds value.

## Summary
- 1283 nodes · 2582 edges · 43 communities detected
- Extraction: 70% EXTRACTED · 30% INFERRED · 0% AMBIGUOUS · INFERRED: 780 edges (avg confidence: 0.5)
- Token cost: 0 input · 0 output

## Community Hubs (Navigation)
- [[_COMMUNITY_BaseDownloader Core|BaseDownloader Core]]
- [[_COMMUNITY_CacheManager|CacheManager]]
- [[_COMMUNITY_Config & Generic Downloader|Config & Generic Downloader]]
- [[_COMMUNITY_FastAPI App & Audit|FastAPI App & Audit]]
- [[_COMMUNITY_Error Classes|Error Classes]]
- [[_COMMUNITY_Downloader Platforms|Downloader Platforms]]
- [[_COMMUNITY_Calibration & Segmentation|Calibration & Segmentation]]
- [[_COMMUNITY_Error Handling|Error Handling]]
- [[_COMMUNITY_Dialog Rendering|Dialog Rendering]]
- [[_COMMUNITY_API Manager|API Manager]]
- [[_COMMUNITY_WeChat Notifications|WeChat Notifications]]
- [[_COMMUNITY_Risk Control|Risk Control]]
- [[_COMMUNITY_Audit Logger|Audit Logger]]
- [[_COMMUNITY_CapsWriter Client|CapsWriter Client]]
- [[_COMMUNITY_Downloader Interface|Downloader Interface]]
- [[_COMMUNITY_User Manager|User Manager]]
- [[_COMMUNITY_Web Views|Web Views]]
- [[_COMMUNITY_ASR Monitor|ASR Monitor]]
- [[_COMMUNITY_Terminology DB|Terminology DB]]
- [[_COMMUNITY_Mobile UI|Mobile UI]]
- [[_COMMUNITY_FunASR Speaker|FunASR Speaker]]
- [[_COMMUNITY_Cache Capabilities|Cache Capabilities]]
- [[_COMMUNITY_LLM Prompt Building|LLM Prompt Building]]
- [[_COMMUNITY_Timezone Helper|Timezone Helper]]
- [[_COMMUNITY_Quality Validation System|Quality Validation System]]
- [[_COMMUNITY_Downloader Architecture|Downloader Architecture]]
- [[_COMMUNITY_Downloader Optimization|Downloader Optimization]]
- [[_COMMUNITY_Module Migration|Module Migration]]
- [[_COMMUNITY_Prompt Engineering|Prompt Engineering]]
- [[_COMMUNITY_Init Files|Init Files]]
- [[_COMMUNITY_Error Creation|Error Creation]]
- [[_COMMUNITY_Performance Tracking|Performance Tracking]]
- [[_COMMUNITY_Tempfile Management|Tempfile Management]]
- [[_COMMUNITY_Tempfile Context|Tempfile Context]]
- [[_COMMUNITY_DB Context|DB Context]]
- [[_COMMUNITY_Init Files|Init Files]]
- [[_COMMUNITY_LLM Config|LLM Config]]
- [[_COMMUNITY_Project Overview|Project Overview]]
- [[_COMMUNITY_YouTube Audio Client|YouTube Audio Client]]
- [[_COMMUNITY_TikHub Xiaohongshu|TikHub Xiaohongshu]]
- [[_COMMUNITY_Web View Feature|Web View Feature]]
- [[_COMMUNITY_Quality Deep-Dive|Quality Deep-Dive]]
- [[_COMMUNITY_Architecture Review|Architecture Review]]

## God Nodes (most connected - your core abstractions)
1. `BaseDownloader` - 81 edges
2. `VideoMetadata` - 80 edges
3. `DownloadInfo` - 80 edges
4. `LLMClient` - 58 edges
5. `YouTubeApiError` - 54 edges
6. `CacheManager` - 51 edges
7. `TempFileManager` - 42 edges
8. `ErrorCode` - 41 edges
9. `LLMConfig` - 40 edges
10. `YouTubeApiClient` - 36 edges

## Surprising Connections (you probably didn't know these)
- `有说话人文本分段器  基于 structured_calibrator.py 的 _intelligent_chunking 逻辑重构` --uses--> `LLMConfig`  [INFERRED]
  src/video_transcript_api/llm/segmenters/dialog_segmenter.py → src/video_transcript_api/llm/core/config.py
- `无说话人文本分段器  基于现有 text_segmentation.py 的分段逻辑重构` --uses--> `LLMConfig`  [INFERRED]
  src/video_transcript_api/llm/segmenters/text_segmenter.py → src/video_transcript_api/llm/core/config.py
- `ValidationInput` --uses--> `LLMClient`  [INFERRED]
  src/video_transcript_api/llm/validators/unified_quality_validator.py → src/video_transcript_api/llm/core/llm_client.py
- `将对话列表格式化为 JSON 字符串（用于日志或调试）` --uses--> `LLMClient`  [INFERRED]
  src/video_transcript_api/llm/validators/unified_quality_validator.py → src/video_transcript_api/llm/core/llm_client.py
- `JSON Schema 定义模块  包含 LLM 结构化输出所需的所有 Schema 定义。` --uses--> `TerminologyDB`  [INFERRED]
  src/video_transcript_api/llm/schemas/__init__.py → src/video_transcript_api/terminology/terminology_db.py

## Hyperedges (group relationships)
- **LLM Three-Layer Architecture** — llm_coordinator, quality_validator, key_info_extractor, speaker_inferencer, summary_processor, text_segmenter, dialog_segmenter [EXTRACTED 1.00]
- **Six Platform Downloaders** — youtube_downloader, bilibili_downloader, douyin_downloader, xiaohongshu_downloader, xiaoyuzhou_downloader, generic_downloader [EXTRACTED 1.00]
- **Sanitization Strategy Application Contexts** — risk_control, three_sanitization_strategies, sensitive_word_detection [INFERRED 0.85]
- **LLM Processing Architecture** — llm_coordinator, plain_text_processor, speaker_aware_processor, key_info_extractor, speaker_inferencer, quality_validator, text_segmenter, dialog_segmenter [EXTRACTED 0.90]
- **Quality Validation System** — unified_quality_validator, validation_input, score_calculator, prompt_builder, dialog_structure_consistency_check [EXTRACTED 0.90]
- **Module Migration Bundle** — module_migration_llm, module_migration_cache, module_migration_risk_control [EXTRACTED 0.85]

## Communities

### Community 0 - "BaseDownloader Core"
Cohesion: 0.03
Nodes (113): BaseDownloader, extract_video_id(), 获取字幕，如果有的话          参数:             url: 视频URL          返回:             str: 字幕文, 兼容旧接口的 video_info 结构          参数:             url: 视频URL          返回:, 将标准化结构转换为旧的 video_info 字典, 解析短链接，获取原始长链接          参数:             url: 短链接URL          返回:             str:, Download file to local temp directory with retry logic.          Args:, 验证文件是否为有效的音视频文件          参数:             file_path: 文件路径          返回: (+105 more)

### Community 1 - "CacheManager"
Cohesion: 0.03
Nodes (82): CacheManager, _get_cursor(), 更新任务的 LLM 模型配置信息          Args:             task_id: 任务ID             llm_co, 获取任务的 LLM 模型配置信息          Args:             task_id: 任务ID          Returns:, 保存说话人映射缓存          Args:             platform: 平台名称             media_id: 媒体, 重建 task_status 表（用于移除 UNIQUE 约束）, 初始化缓存管理器          Args:             cache_dir: 缓存目录路径（与现有系统一致）, 初始化缓存管理器                  Args:             cache_dir: 缓存文件目录             db (+74 more)

### Community 2 - "Config & Generic Downloader"
Cohesion: 0.03
Nodes (78): BaseModel, Config, GenericDownloader, _build_calibration_warning(), _build_result_dict(), _detect_risk(), _generate_title_if_needed(), _handle_llm_task() (+70 more)

### Community 3 - "FastAPI App & Audit"
Cohesion: 0.03
Nodes (57): get_audit_logger(), get_cache_manager(), get_config(), get_executor(), get_llm_coordinator(), get_llm_executor(), get_llm_queue(), get_logger() (+49 more)

### Community 4 - "Error Classes"
Cohesion: 0.05
Nodes (47): VideoTranscriptAPI 项目级错误基类      所有自定义异常都应继承此类，便于统一捕获和分类。      Attributes:, TranscriptAPIError, yt-dlp configuration builder.  This module provides a builder class for constr, Check if cookie is available for use.          Returns:             True if c, Check if fallback to cookie-less mode is allowed.          Returns:, Get the cookie file path if available.          Returns:             Absolute, Get player client list from config or use defaults.          Returns:, Build base yt-dlp options without cookie.          Returns:             Dicti (+39 more)

### Community 5 - "Downloader Platforms"
Cohesion: 0.03
Nodes (65): Audit Logging, BBDown, BilibiliDownloader, Bilibili Metadata Enhancement, Bilibili Official API, calibrate_model, CALIBRATION_RESULT_SCHEMA, CapsWriter-Offline (+57 more)

### Community 6 - "Calibration & Segmentation"
Cohesion: 0.05
Nodes (24): 校对结果 JSON Schema  用于 structured_calibrator.py 中的对话校对输出格式定义。, 有说话人文本分段器  基于 structured_calibrator.py 的 _intelligent_chunking 逻辑重构, classify_error(), 错误分类模块  提供 LLM 错误分类功能，区分可重试和不可重试的错误, 将异常分类为具体的错误类型      分类优先级：Fatal > Timeout > Truncation > Retryable      Args:, build_key_info_user_prompt(), from_dict(), 构建关键信息提取的用户提示词      Args:         title: 视频标题         author: 作者/频道 (+16 more)

### Community 7 - "Error Handling"
Cohesion: 0.06
Nodes (45): FatalError, LLMError, 可重试错误      包括：超时、服务器错误、速率限制等, 超时错误      网络连接超时或读取超时，重试可能无效（同样的请求大概率同样超时）, 输出截断错误      模型输出 token 耗尽导致 JSON 被截断，重试无意义（同样的输入产生同样的截断）, 不可重试错误      包括：认证失败、权限拒绝、资源不存在、配置错误等, RetryableError, TimeoutError (+37 more)

### Community 8 - "Dialog Rendering"
Cohesion: 0.07
Nodes (28): DialogRenderer, 对话内容渲染器     支持两种模式：     1. 多人对话模式：自动检测说话人，应用对话样式     2. 普通文本模式：无说话人识别，使用常规样式, 获取说话人的颜色          Args:             speaker: 说话人姓名             speaker_list: 所有说, 智能分段，通过中英文标点符号自动分段          Args:             text: 输入文本          Returns:, 渲染对话为HTML格式          Args:             text: 输入文本          Returns:, 渲染普通文本，支持Markdown格式          Args:             text: 文本内容          Returns:, 根据缓存目录选择最优渲染策略         简化为 3 种策略：structured（FunASR V2）, capswriter_long_text（Cap, 检测文本是否为多人对话格式          Args:             text: 输入文本          Returns: (+20 more)

### Community 9 - "API Manager"
Cohesion: 0.1
Nodes (14): APIManager, bindURLSelection(), copyToClipboard(), getSelectedURL(), handleTextInput(), initializePage(), simpleDecrypt(), simpleEncrypt() (+6 more)

### Community 10 - "WeChat Notifications"
Cohesion: 0.08
Nodes (30): format_llm_config_markdown(), _get_global_notifier(), _get_risk_control(), init_global_notifier(), 恢复被保护的 URL          参数:             content: 处理后的内容             url_map: URL, 初始化全局 WeComNotifier 实例      应在应用启动时调用一次，确保所有通知共享同一个实例，     从而实现正确的并发控制和消息顺序保证, 安全的风控处理，保护 URL 不被误处理          参数:             content: 原始内容             text, 发送文本消息（兼容方法，内部调用send_markdown_v2）          参数:             content: 要发送的文本内容 (+22 more)

### Community 11 - "Risk Control"
Cohesion: 0.07
Nodes (22): init_risk_control(), is_enabled(), 初始化风控模块      Args:         config: 配置字典, 对文本进行敏感词检测和消敏处理      Args:         text: 待处理的文本         text_type: 文本类型, sanitize_text(), 敏感词库管理器  负责： 1. 从配置的URL列表下载敏感词库 2. 合并所有词库并去重 3. 保存到本地缓存 4. 下载失败时使用本地缓存, 解析敏感词文本内容          Args:             content: 文本内容          Returns:, 保存敏感词库到本地缓存          Args:             words: 敏感词集合          Returns: (+14 more)

### Community 12 - "Audit Logger"
Cohesion: 0.08
Nodes (21): AuditLogger, get_audit_logger(), _get_cursor(), API调用审计日志模块  提供API调用统计和审计功能，支持多用户监控。 使用线程本地存储复用数据库连接，内置 schema 版本迁移系统。, 设置 schema 版本号          Args:             cursor: 数据库游标             version: 要设置的, 检查 schema 版本并按需执行迁移          Args:             cursor: 数据库游标, 对API密钥进行脱敏处理          Args:             api_key: 原始API密钥          Returns:, 记录API调用日志          Args:             api_key: API密钥             user_id: 用户ID (+13 more)

### Community 13 - "CapsWriter Client"
Cohesion: 0.11
Nodes (20): _build_token_position_map(), CapsWriterClient, _clean_token(), _create_segments_from_capswriter(), _find_token_idx(), load_from_project_config(), main(), _optimize_segment_lengths() (+12 more)

### Community 14 - "Downloader Interface"
Cohesion: 0.14
Nodes (11): ABC, _fetch_download_info(), _fetch_metadata(), get_temp_manager(), _enrich_from_widgets_context(), _extract_all_matches(), _extract_by_path(), _extract_first_match() (+3 more)

### Community 15 - "User Manager"
Cohesion: 0.1
Nodes (13): get_user_manager(), 用户配置管理模块  提供多用户配置的加载、验证和管理功能。, 根据用户ID获取用户信息                  Args:             user_id: 用户ID, 获取用户的企业微信webhook地址                  Args:             token: API令牌, 获取所有用户列表（不包含API密钥）                  Returns:             list: 用户信息列表, 对API密钥进行脱敏处理                  Args:             api_key: 原始API密钥, 检查用户是否有指定权限          legacy 单 token 用户默认拥有所有权限；         多用户模式下检查 permissions, 初始化用户管理器                  Args:             users_config_path: 用户配置文件路径，默认为 c (+5 more)

### Community 16 - "Web Views"
Cohesion: 0.11
Nodes (23): _build_metadata_headers(), _build_page_html(), _build_text_metadata_header(), export_content(), generate_download_filename(), handle_page_export(), handle_raw_export(), home() (+15 more)

### Community 17 - "ASR Monitor"
Cohesion: 0.13
Nodes (12): ASRMonitor, ASR 服务监控模块  后台定时检查 CapsWriter/FunASR 服务状态，连续失败时发送企微告警， 恢复后发送恢复通知。内置防抖机制（告警后 30 分, 处理检查结果，决定是否告警          Args:             name: 服务名称             url: 服务 URL, 获取配置时区的当前时间字符串          Returns:             格式化的本地时间字符串 (YYYY-MM-DD HH:MM:SS), 发送告警（带防抖）          Args:             name: 服务名称             url: 服务 URL, 发送恢复通知          Args:             name: 服务名称             url: 服务 URL, 发送企微通知          Args:             message: 通知内容, 从配置启动 ASR 监控器      Args:         config: 应用配置字典      Returns:         ASRMonitor (+4 more)

### Community 18 - "Terminology DB"
Cohesion: 0.14
Nodes (11): get_terminology_db(), 专有名词库管理模块  提供从 JSON 文件加载专有名词、查询匹配项、生成 LLM prompt 注入文本的功能。 支持默认词库和用户自定义词库。, 将专有名词列表格式化为 LLM prompt 注入文本          Args:             matched_terms: 匹配到的专有名词列表, 查询文本中的匹配项并格式化为 prompt 注入文本          Args:             text: 待查询的文本（通常是转录原文或元数据）, 获取全局专有名词库实例（单例模式）      Args:         custom_path: 用户自定义词库路径      Returns:, 专有名词库      从 JSON 文件加载专有名词对照表，提供查询和 prompt 注入功能。      Attributes:         terms:, 初始化专有名词库          Args:             custom_path: 用户自定义词库路径，为 None 时仅使用默认词库, 加载用户自定义专有名词库          Args:             path: 自定义词库文件路径 (+3 more)

### Community 19 - "Mobile UI"
Cohesion: 0.18
Nodes (15): bindEvents(), checkMobile(), closeMobilePanel(), createMobileTocHTML(), createPCTocHTML(), extractHeadings(), findCalibratedSection(), handlePinClick() (+7 more)

### Community 20 - "FunASR Speaker"
Cohesion: 0.24
Nodes (4): FunASRSpeakerClient, 使用说话人识别功能进行转录          参数:             audio_path: 音频文件路径             output, 格式化带说话人的转录文本          参数:             transcription_result: FunASR服务器返回的转录结果, 同步接口：使用说话人识别功能进行转录          参数:             audio_path: 音频文件路径             o

### Community 21 - "Cache Capabilities"
Cohesion: 0.18
Nodes (6): analyze_cache_capabilities(), CacheCapabilities, CacheCapabilityAnalyzer, 便捷函数：分析单个缓存目录的能力      Args:         cache_dir: 缓存目录路径      Returns:, 缓存能力分析器     负责分析缓存目录的引擎类型和说话人支持能力, 分析缓存目录的完整能力信息          Args:             cache_dir: 缓存目录路径          Returns

### Community 22 - "LLM Prompt Building"
Cohesion: 0.13
Nodes (14): build_calibrate_user_prompt(), build_speaker_inference_user_prompt(), build_structured_calibrate_user_prompt(), build_summary_user_prompt(), build_validation_user_prompt(), 构建总结任务的 User Prompt      Args:         transcript: 转录文本         video_title:, 构建结构化校对任务的 User Prompt      支持两种调用方式：     1. 旧版：传入 input_data (dict) - 用于向后兼容, 构建校验任务的 User Prompt      Args:         original_data: 原始数据         calibrate (+6 more)

### Community 23 - "Timezone Helper"
Cohesion: 0.24
Nodes (10): format_datetime_for_display(), format_datetime_with_timezone(), get_configured_timezone(), get_current_time_display(), parse_timezone_offset(), 为web页面显示格式化时间          Args:         dt_str: 数据库中的时间字符串              Return, 获取当前时间的显示格式          Returns:         当前时间的格式化字符串, 获取配置的时区对象          Returns:         配置的timezone对象，默认为UTC+8 (+2 more)

### Community 24 - "Quality Validation System"
Cohesion: 0.4
Nodes (5): Dialog Structure Consistency Check, PromptBuilder, ScoreCalculator, UnifiedQualityValidator, ValidationInput

### Community 25 - "Downloader Architecture"
Cohesion: 0.67
Nodes (3): BaseDownloader Interface Standardization, DownloadInfo, VideoMetadata

### Community 26 - "Downloader Optimization"
Cohesion: 0.67
Nodes (3): Downloader Instance Lifecycle Management, Instance-Level Caching, YouTube Downloader Optimization

### Community 27 - "Module Migration"
Cohesion: 0.67
Nodes (3): Cache Module Migration, LLM Module Migration, RiskControl Module Migration

### Community 28 - "Prompt Engineering"
Cohesion: 1.0
Nodes (2): Prefix Cache Optimization, Prompt Engineering

### Community 29 - "Init Files"
Cohesion: 1.0
Nodes (0): 

### Community 30 - "Error Creation"
Cohesion: 1.0
Nodes (1): 从 API 响应的 error 字段创建错误对象          Args:             error_data: API 响应中的 erro

### Community 31 - "Performance Tracking"
Cohesion: 1.0
Nodes (1): 记录指定阶段的耗时          Args:             stage: 阶段名称（如 "download", "transcribe", "ll

### Community 32 - "Tempfile Management"
Cohesion: 1.0
Nodes (1): 上下文管理器：创建临时目录并在退出时清理          Args:             prefix: 目录前缀          Yields:

### Community 33 - "Tempfile Context"
Cohesion: 1.0
Nodes (1): 上下文管理器：创建临时文件并在退出时清理          Args:             suffix: 文件后缀             prefix:

### Community 34 - "DB Context"
Cohesion: 1.0
Nodes (1): 获取数据库游标的上下文管理器，自动 commit/rollback

### Community 35 - "Init Files"
Cohesion: 1.0
Nodes (0): 

### Community 36 - "LLM Config"
Cohesion: 1.0
Nodes (1): 从配置字典创建 LLMConfig 实例          Args:             config_dict: 完整的配置字典          Re

### Community 37 - "Project Overview"
Cohesion: 1.0
Nodes (1): Project Overview Screenshot

### Community 38 - "YouTube Audio Client"
Cohesion: 1.0
Nodes (1): YouTubeAudioClient

### Community 39 - "TikHub Xiaohongshu"
Cohesion: 1.0
Nodes (1): TikHub Xiaohongshu API

### Community 40 - "Web View Feature"
Cohesion: 1.0
Nodes (1): Web View

### Community 41 - "Quality Deep-Dive"
Cohesion: 1.0
Nodes (1): Quality Deep-Dive Blueprint

### Community 42 - "Architecture Review"
Cohesion: 1.0
Nodes (1): Architecture Review

## Knowledge Gaps
- **299 isolated node(s):** `专有名词库管理模块  提供从 JSON 文件加载专有名词、查询匹配项、生成 LLM prompt 注入文本的功能。 支持默认词库和用户自定义词库。`, `专有名词库      从 JSON 文件加载专有名词对照表，提供查询和 prompt 注入功能。      Attributes:         terms:`, `初始化专有名词库          Args:             custom_path: 用户自定义词库路径，为 None 时仅使用默认词库`, `加载用户自定义专有名词库          Args:             path: 自定义词库文件路径`, `从 JSON 文件加载专有名词          Args:             path: JSON 文件路径             source: 来` (+294 more)
  These have ≤1 connection - possible missing edges or undocumented components.
- **Thin community `Prompt Engineering`** (2 nodes): `Prefix Cache Optimization`, `Prompt Engineering`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Init Files`** (1 nodes): `__init__.py`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Error Creation`** (1 nodes): `从 API 响应的 error 字段创建错误对象          Args:             error_data: API 响应中的 erro`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Performance Tracking`** (1 nodes): `记录指定阶段的耗时          Args:             stage: 阶段名称（如 "download", "transcribe", "ll`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Tempfile Management`** (1 nodes): `上下文管理器：创建临时目录并在退出时清理          Args:             prefix: 目录前缀          Yields:`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Tempfile Context`** (1 nodes): `上下文管理器：创建临时文件并在退出时清理          Args:             suffix: 文件后缀             prefix:`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `DB Context`** (1 nodes): `获取数据库游标的上下文管理器，自动 commit/rollback`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Init Files`** (1 nodes): `__init__.py`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `LLM Config`** (1 nodes): `从配置字典创建 LLMConfig 实例          Args:             config_dict: 完整的配置字典          Re`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Project Overview`** (1 nodes): `Project Overview Screenshot`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `YouTube Audio Client`** (1 nodes): `YouTubeAudioClient`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `TikHub Xiaohongshu`** (1 nodes): `TikHub Xiaohongshu API`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Web View Feature`** (1 nodes): `Web View`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Quality Deep-Dive`** (1 nodes): `Quality Deep-Dive Blueprint`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Architecture Review`** (1 nodes): `Architecture Review`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `YouTubeApiError` connect `BaseDownloader Core` to `Config & Generic Downloader`, `Error Handling`?**
  _High betweenness centrality (0.225) - this node is a cross-community bridge._
- **Why does `JSON Schema 定义模块  包含 LLM 结构化输出所需的所有 Schema 定义。` connect `Error Classes` to `CacheManager`, `FastAPI App & Audit`, `Calibration & Segmentation`, `Error Handling`, `Risk Control`, `Terminology DB`, `LLM Prompt Building`?**
  _High betweenness centrality (0.218) - this node is a cross-community bridge._
- **Why does `LLMCallError` connect `Error Handling` to `CacheManager`, `Error Classes`?**
  _High betweenness centrality (0.173) - this node is a cross-community bridge._
- **Are the 69 inferred relationships involving `BaseDownloader` (e.g. with `BilibiliDownloader` and `判断是否可以处理该URL          参数:             url: 视频URL          返回:             bool:`) actually correct?**
  _`BaseDownloader` has 69 INFERRED edges - model-reasoned connections that need verification._
- **Are the 79 inferred relationships involving `VideoMetadata` (e.g. with `BilibiliDownloader` and `判断是否可以处理该URL          参数:             url: 视频URL          返回:             bool:`) actually correct?**
  _`VideoMetadata` has 79 INFERRED edges - model-reasoned connections that need verification._
- **Are the 79 inferred relationships involving `DownloadInfo` (e.g. with `BilibiliDownloader` and `判断是否可以处理该URL          参数:             url: 视频URL          返回:             bool:`) actually correct?**
  _`DownloadInfo` has 79 INFERRED edges - model-reasoned connections that need verification._
- **Are the 53 inferred relationships involving `LLMClient` (e.g. with `LLMCoordinator` and `LLM 处理协调器      负责场景路由，统一入口接口，集成两个处理器`) actually correct?**
  _`LLMClient` has 53 INFERRED edges - model-reasoned connections that need verification._