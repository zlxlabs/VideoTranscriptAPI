# DESIGN-note：#159 音轨准入 / #147 配置生效值 / #156 ASCII 句末 —— 三项独立根治

> 基线 `main@2995a379`。三项互不依赖，可按任意顺序拆卡实现；本文件不实现运行时代码。

## 目标

1. **#159**：无音轨媒体在进入任何 ASR 引擎**之前**被拒，用户收到写明原因的任务失败通知，而不是笼统的「下载文件失败」或一份空转写。
2. **#147**：`python main.py --check-config` 除 `Configuration OK` 外，打印 10 个 LLM 参数的**生效值 + 来源**（配置文件 / 代码默认 / 派生），机器可解析，供"生产跑的是几"这类问题当场自答。
3. **#156**：长对话分块处补齐 ASCII `!?` 句末，使纯英文长文本不再退化成整块硬切；中文既有切分结果逐项不变。

## 非目标

不改生产取值与示例取值（只更正示例里失效的注释）；不统一全仓 4 套句末定义（#146/#151/#152 的暂缓决定保持）；不新增英文句点 `.` 识别；不恢复"统一抽音轨"；不为未知配置键加拒绝；不引入 `last_error` 类跨请求状态；不建路线生成器或项目进度系统；不部署。

## 为什么不是分区 / 删除 / 约定

- **分区**（让冲突消失）：#159 恰恰**不能**靠分区解决。五条下载路径（`transcription.py:2236` generic / `:2251` 预下载本地文件 / `:2274` YouTube yt-dlp / `:2279` base / `bilibili.py:391` bbdown）不共用任何判定，把规则分区到各下载器等于重造 N 套。真正的"消失"来自收口到一个共享准入点。
- **删除**（砍掉不可证的部分）：#159 只删掉 `base.py:417` 的 ` and not has_video` 不够——归因仍在 `base.py:293-296` 的 `except InvalidMediaError: return None` 处被截断，删完用户仍只看到「下载文件失败」。#147 不能删掉 `from_dict` 派生层，那是 `test_llm_config_defaults.py` 锁死的既有行为。
- **约定**（用命名/文档代替运行时机制）：#156 是运行时切分行为，约定代替不了；#147 的失败恰恰是"注释当契约"——`config/config.example.jsonc:273` 写 `>=`，实现 `plain_text_processor.py:121` 是 `>`，注释已经骗不到人了。

## 方案要点与已否决方案

### 增量一 · #159 音轨准入与用户归因

**要点**

1. **收口点**：真实共享的准入边界是「`local_file` 已就绪、即将进 ASR」那一刻，共两处：`api/services/transcription.py:2287-2296`（常规路径，`if not local_file` 之后、`media_duration` 读取处）与 `transcription.py:1903-1922`（`youtube-api` 分支，`api_result["audio_path"]` 之后）。此点覆盖全部五条下载路径。
2. **判定函数必须与 downloader 实例无关**：`transcription.py:2296` 已记录「已有本地文件」分支下 `actual_downloader` 为 `None`，所以准入判定只能是无状态的模块级函数，不能是 `BaseDownloader` 的实例方法。
3. **三态语义**：`has_audio` / `no_audio` / `probe_unknown`。只有 `no_audio` 拒绝；`probe_unknown`（ffprobe 缺失、超时、返回码非 0、JSON 解析失败）记 warning 后放行，保持 `base.py:449-464` 的现有可用性语义，但**任何日志与返回值都不得把 unknown 记成"有音轨"**。
4. **归因**：复用既有 `InvalidMediaError`（`errors/download.py:16`）承载原因，在准入点捕获后走同一条 `_fail_task_and_notify`，把「该媒体不含音轨，无法转录」写进 `error_msg`。
5. **base 侧判定条件不改**：`_validate_media_file` 保持 `not has_audio and not has_video`。改它会让 `bilibili.py:391` 的 bbdown 预检在准入点之前就触发重试，形成两套规则；音轨准入只存在于共享准入点这一处。
6. **重复探测**：接受一次额外 ffprobe。已实测本地元数据探测 20–30 ms（10 KB mp4，5 次）。不复用 base 的探测结果——它挂在**任务生命周期内、可能已被丢弃或换实例**的 downloader 上；也不引入缓存。同一准入点对同一 `local_file` 只探测一次。不承诺省下首次下载。
7. 音视频混合文件照常放行，**不抽音轨**。

**已否决**

- 只改 `base.py:417` 判定条件：覆盖不到 generic / yt-dlp / 预下载三条路径（`generic.py` 全文无 `_validate_media_file` / `ffprobe`；`youtube.py:1134` 走 yt-dlp 主链路）。
- 让 `download_file` 返回 `(path, reason)` 或加 `last_error` 实例属性：牵动 5 个调用点与 `download_info.local_file` 模型，且撞锁定决策（禁跨请求状态）。
- 给每个下载器各加一套音轨检查：等于重造分区，规则必然漂移。
- `ffmpeg -vn` 抽音轨：锁定决策已否决，且会把准入问题变成转码问题。

### 增量二 · #147 有效 LLM 参数及来源诊断

**要点**

1. **形态**：`main.py` 在现有 `print("Configuration OK")` **之后**追加一行 JSON（`json.dumps(..., ensure_ascii=False, sort_keys=True)`，复用标准库）。现有断言 `"Configuration OK" in stdout` 的测试不受影响。
2. **取值唯一来源**：`main.py` 已从 `load_and_validate_config` 拿到校验后的 dict（`api/context.py:565-593` 返回 `validated`），再过一次 `LLMConfig.from_dict` 即得与启动路径**同一个解析器**的真实生效值。不写第二套手算默认值。
3. **白名单 10 键**及原始路径：`enable_threshold` / `segment_size` / `max_segment_size` ← `llm.segmentation.*`；`min_chunk_length` / `max_chunk_length` / `preferred_chunk_length` / `calibration_concurrent_limit` ← `llm.structured_calibration.*`；`structured_calibration_for_plain` ← `llm.*`；`structured_fallback_strategy` ← `llm.structured_calibration.quality_validation.fallback_strategy`。
4. **来源判定**：键在该路径上存在**且非 None、非空字符串** → `config`；不存在 → `default`；`structured_fallback_strategy` 未命中但 `llm.structured_calibration.fallback_to_original` 命中 → `derived`（对应 `llm/core/config.py:190-195`）。字段存在但为 `null`/`""` 时**不得**标 `config`，因为它实际走的是派生分支。
5. **凭据零泄漏**：白名单内没有任何 `api_key` / `base_url` / `*_model` / token / cookie / url；测试须断言输出文本不含配置文件里的 `api_key` 值。
6. **注释修正**：`config/config.example.jsonc:273` 的 `>=` 改为与 `plain_text_processor.py:121`（`len(text) > self.config.enable_threshold`）一致的措辞。**只改注释文字，不动任何数值。**

**已否决**

- `json.dumps` 整个配置：会打印 api_key / webhook / cookie。
- 新建通用"配置来源登记表"或标注框架：没有第二消费者，撞反过度设计红线。
- 统一 dataclass 默认 / 示例 / 生产三套取值：锁定决策否决；本单只观测不动。
- 顺带给 `validate_config` 加取值校验：属延后项，另开卡。

### 增量三 · #156 ASCII `!?` 局部切分

**要点**

1. **唯一改动点**：`llm/segmenters/dialog_segmenter.py:338` 的 `re.split(r'([。！？])', text)` → `([。！？!?])`。重组逻辑不动，**不补 `.`**。
2. 硬上限兜底（`_split_long_dialog` → `split_oversized_text`）与 `max_chunk_length` 语义不动。
3. **断言分两层**：
   - `_split_by_sentences` 返回值：断言新增标点不丢（无空白尾片的样本 `''.join(result) == text` 成立）。
   - `_split_long_dialog` / `segment()`：只断言中文样本**逐项不变**、时间戳单调且首尾锚定。**不承诺全链路字符守恒**——`:346` 的 `if sentence.strip()` 与 `:131`/`:141` 的 `.strip()` 都会丢空白。
4. 真实入口是 `speaker_aware_processor.py:61`（有说话人）与 `:247`（plain 路径，用 `plain_structured_*` 覆盖长度参数）两条。句子数量 ≠ LLM 调用次数，调用数由 `segment()` 的打包层决定。

**已否决**

- 顺带补 `.`：会切碎英文缩写与小数点，且锁定决策明确只补 `!?`。
- 统一全仓 4 套句末定义（`capswriter_client` / `paragraphize` / `DialogSegmenter` / `TextSegmenter`）：#151/#152 暂缓决定不动，本单只改其中一处并在注释里更新对照表。

## 候选文件边界（供主脑拆实现卡）

| 增量 | 拟改文件 | 拟增/改测试 |
| --- | --- | --- |
| #159 | `src/video_transcript_api/api/services/transcription.py`（准入点 + 归因文案）；`src/video_transcript_api/downloaders/media_probe.py`（新，无状态探测函数 + 三态结果）；`src/video_transcript_api/downloaders/base.py`（仅在需要复用 ffprobe 调用形态时改，不改判定条件） | `tests/unit/test_transcription_audio_admission.py`（新：lavfi 现场生成 video-only / 有声 / 混合三样本，断言拒绝文案 + 引擎零调用）；`tests/unit/test_downloader_core.py`（保留既有 3 个 ffprobe 用例不动） |
| #147 | `main.py`；`src/video_transcript_api/llm/core/config.py`（新增纯函数产出 10 键的 value+source）；`config/config.example.jsonc`（仅 `:273` 注释） | `tests/unit/test_llm_config_defaults.py`（扩来源标签用例）；`tests/unit/test_runtime_lifecycle.py::test_check_config_is_side_effect_free` 邻侧新增 subprocess 用例（无会话 env、`env=` 显式最小集），断言 JSON 行可解析、含 10 键、含 `api_key` 不出现 |
| #156 | `src/video_transcript_api/llm/segmenters/dialog_segmenter.py`（仅正则 + 注释对照表） | `tests/unit/test_dialog_segmenter_caps.py`（扩：ASCII `!?` 样本逐项断言；中文样本与 `test_punctuation_split_behaviour_is_unchanged` 邻证不变；`_split_long_dialog` 时间戳单调 + 首尾锚定） |

## 关键不变式

1. [实测] 无音轨纯视频**今天会被放行**：`base.py:417` 的 `not has_audio and not has_video`。本 worktree 用 lavfi 现场生成 `video_only.mp4` 实跑 `_validate_media_file`，日志为「文件验证通过: 音频流=False, 视频流=True」且写出 `duration=2.0`。本批需新增测试把"被拒"锁死。
2. [实测] 归因断点在 `base.py:293-296`：`except InvalidMediaError` 直接 `return None`，异常消息永不外传；用户可见文本唯一汇流点是 `transcription.py:2287` 的 `下载文件失败: {url}`。
3. [实测] `actual_downloader` 在「已有本地文件」分支（`transcription.py:2251-2253`）未被赋值，`transcription.py:2296` 读到 `None` —— 准入判定不能依赖实例状态。
4. [实测] ffprobe 本地元数据探测（`-v quiet -print_format json -show_format -show_streams`）在本机对 10 KB mp4 耗时 0.02–0.03 s。
5. [实测] `main.py:27-31` 跑完全部启动期演练后只 `print("Configuration OK")`。
6. [实测] 示例注释 `config/config.example.jsonc:273` 写 `>=`，实现 `plain_text_processor.py:121` 是严格大于。
7. [实测] `_split_by_sentences` 现状：`Is it ok? Yes! Great news! Really? Wow!` → 1 段；`今天很好。明天呢？后天啊！` → 3 段；`句一。\n\n。句二。` → 3 段且中间片 `'\n\n。'` 不丢；`第一句。   ` → 不守恒（`if sentence.strip()` 丢空白尾片）。
8. [实测] `tests/unit/test_llm_config_defaults.py` 已锁死 `from_dict` 与 dataclass 直构逐字段一致（#158）——`#147` 的取值来源必须继续走 `from_dict`，不得旁路。
9. [实测] `tests/unit/test_runtime_lifecycle.py::test_check_config_is_side_effect_free` 已用 subprocess 跑 `main.py --check-config` 并断言无副作用目录生成。

## 待验证前提

1. [推断] ASR 入口只有 `transcription.py` 这两处：需在实现卡里用 `rg -n "transcribe_sync|CapsWriterClient\(|funasr" src/` 复核有无第三条直调入口（API handler 或其他 service）。
2. [推断] `probe_unknown` 放行符合用户预期（宁可空转写也不误杀正常媒体）：需 owner 拍板；若改判为 fail fast，影响面是所有缺 ffprobe 的部署。
3. [推断] 30 MB 级真实媒体 ffprobe 仍在百毫秒内：本次只量了 10 KB 样本，未取真实媒体。
4. [推断] `youtube-api` 分支的 `audio_path` 必为音频文件，准入探测不会给正常任务带来误拒：该值由外部 API Server 产出，本仓无法静态证明。
5. [推断] `_split_by_sentences` 只在 `_split_long_dialog`（`:125`）被调用，故本改动的实际生效面仅限超长单条对话。

## 验收路径

1. **入口**：#159 —— 提交一个无音轨纯视频 URL 的转录请求（API 层，与生产同入口）。
   **步骤**：任务进入下载 → 准入点 → ASR。
   **预期**：失败通知文本指明「不含音轨」而非「下载文件失败」；任务终态 FAILED；日志显示两个引擎（FunASR / CapsWriter）均未被调用；临时文件按既有清理路径回收。
2. **入口**：#147 —— 在**无会话身份的环境**跑 `env -i PATH=/usr/bin:/bin <python> main.py --check-config --config <path>`。
   **步骤**：读 stdout 最后一行 JSON。
   **预期**：首行仍是 `Configuration OK`；JSON 行含且仅含 10 个白名单键，各带 `value` 与 `source`；输出中不出现配置文件里的 `api_key` 值；两种取值来源（改配置 / 删配置键）分别得到 `config` 与 `default`/`derived`。
3. **入口**：#156 —— plain 与 dialog 两条完整分块路径（`speaker_aware_processor.py:61` 与 `:247`），输入含 ASCII `!?` 的超长文本。
   **步骤**：走 `segment()` → `_split_long_dialog` → `_split_by_sentences`。
   **预期**：英文样本被切成多块且时间戳单调、首尾等于原 dialog 的起止；中文样本输出与改动前逐项相同；现有 `test_punctuation_split_behaviour_is_unchanged` 仍绿。
