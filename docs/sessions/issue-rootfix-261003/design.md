# DESIGN-note：#159 音轨准入 / #147 配置生效值 / #156 ASCII 句末 —— 三项独立根治

> 基线 `main@2995a379`。三项互不依赖，可按任意顺序拆卡实现；本文件不实现运行时代码。

## 目标

1. **#159**：媒体只有在**成功确认至少一条音轨**时才进入任一 ASR 引擎；无音轨、探测工具缺失、超时、非零退出、非法 JSON 一律任务失败，且失败文案区分「不含音轨」与「检查失败」，不谎称输入无音轨。全程只用既有 `InvalidMediaError` 携带具名原因，不新增结果对象或跨请求状态。
2. **#147**：`python main.py --check-config` 除 `Configuration OK` 外，打印 9 个 LLM 参数的**生效值 + 来源**（配置显式值 / 代码缺省 / 派生），机器可解析，供"生产跑的是几"这类问题当场自答。
3. **#156**：长对话分块处补齐 ASCII `!?` 句末，使纯英文长文本不再退化成整块硬切；中文既有切分结果逐项不变。

## 非目标

不改生产取值与示例取值（只更正示例里失效的注释）；不统一全仓 4 套句末定义（#146/#151/#152 的暂缓决定保持）；不新增英文句点 `.` 识别；不恢复"统一抽音轨"；不为未知配置键加拒绝；不引入 `last_error` 类跨请求状态；不为探测失败保留放行或降级分支；不改 `base` 下载器既有校验语义（最终准入在共享点严格且统一）；不修 `_split_by_sentences` 的空白过滤与上层 `.strip()` 行为；不建路线生成器或项目进度系统；不部署、不改 CI workflow。

## 为什么不是分区 / 删除 / 约定

- **分区**（让冲突消失）：#159 恰恰**不能**靠分区解决。五条下载路径（`transcription.py:2236` generic / `:2251` 预下载本地文件 / `:2274` YouTube yt-dlp / `:2279` base / `bilibili.py:391` bbdown）不共用任何判定，把规则分区到各下载器等于重造 N 套。真正的"消失"来自收口到一个共享准入点。
- **删除**（砍掉不可证的部分）：#159 只删掉 `base.py:417` 的 ` and not has_video` 不够——归因仍在 `base.py:293-296` 的 `except InvalidMediaError: return None` 处被截断，删完用户仍只看到「下载文件失败」。#147 不能删掉 `from_dict` 派生层，那是 `test_llm_config_defaults.py` 锁死的既有行为。
- **约定**（用命名/文档代替运行时机制）：#156 是运行时切分行为，约定代替不了；#147 的失败恰恰是"注释当契约"——`config/config.example.jsonc:273` 写 `>=`，实现 `plain_text_processor.py:121` 是 `>`，注释已经骗不到人了。

## 方案要点与已否决方案

### 增量一 · #159 音轨准入与用户归因

**要点**

1. **收口点**：真实共享的准入边界是「`local_file` 已就绪、即将进 ASR」那一刻，共两处：`api/services/transcription.py:2287-2296`（常规路径，`if not local_file` 之后、`media_duration` 读取处）与 `transcription.py:1903-1922`（`youtube-api` 分支，`api_result["audio_path"]` 之后）。这两处就是探测函数的**全部真实引用**，其后的 ASR 分叉（FunASR `:2318`、CapsWriter `:2330` 起）都在准入点下游。此点覆盖全部五条下载路径。
2. **判定函数必须与 downloader 实例无关**：`transcription.py:2296` 已记录「已有本地文件」分支下 `actual_downloader` 为 `None`，所以准入判定只能是无状态的模块级函数，不能是 `BaseDownloader` 的实例方法。落点选 `api/services/transcription.py` 内的模块级私有函数：两个真实引用点都在这一个文件里，不新建模块、不新建通用媒体探测框架。
3. **严格准入（已定，非待议）**：只有"成功确认至少一条音轨"才放行。其余一切情形都抛 `InvalidMediaError` 终止任务：
   - 探测到 0 条 `codec_type == "audio"` 流 → 原因 `no_audio_track`，文案「该媒体不含音轨，无法转录」；
   - ffprobe 不存在（`FileNotFoundError`）、超时、非零退出、stdout 非合法或缺 `streams` 的 JSON、文件不存在 → 原因 `media_probe_failed`，文案「无法校验媒体音轨（检查失败），请重试或换源」，**不得**写成"不含音轨"。

   根治准入不允许"探测失败就放行"——那会把无法证伪的输入当成合格输入，正是本 issue 要消灭的那类假成功。无 fallback、无 warning 后放行、无扩展名兜底。
4. **归因**：复用既有 `InvalidMediaError`（`errors/download.py:16`）作为唯一载体，异常消息带上述具名原因；在两个准入点捕获后走同一条 `_fail_task_and_notify`，把原因写进 `error_msg`。不新增异常类、不新增枚举/结果对象。
5. **base 侧判定条件不改**：`_validate_media_file` 保持 `not has_audio and not has_video`，其 ffprobe 缺失时的扩展名兜底（`base.py:449-464`）作为下载层既有语义保留。改它会让 `bilibili.py:391` 的 bbdown 预检在准入点之前就触发重试，形成两套规则；音轨准入只存在于共享准入点这一处，且比下载层更严。
6. **重复探测**：接受一次额外 ffprobe。已实测本地元数据探测 20–30 ms（10 KB mp4，5 次）。不复用 base 的探测结果——它挂在**任务生命周期内、可能已被丢弃或换实例**的 downloader 上；也不引入缓存。同一准入点对同一 `local_file` 只探测一次。不承诺省下首次下载。
7. 音视频混合文件照常放行，**不抽音轨**。

**已否决**

- 只改 `base.py:417` 判定条件：覆盖不到 generic / yt-dlp / 预下载三条路径（`generic.py` 全文无 `_validate_media_file` / `ffprobe`；`youtube.py:1134` 走 yt-dlp 主链路）。
- 让 `download_file` 返回 `(path, reason)` 或加 `last_error` 实例属性：牵动 5 个调用点与 `download_info.local_file` 模型，且撞锁定决策（禁跨请求状态）。
- 给每个下载器各加一套音轨检查：等于重造分区，规则必然漂移。
- `ffmpeg -vn` 抽音轨：锁定决策已否决，且会把准入问题变成转码问题。
- **探测失败放行 / 三态结果对象（上一稿方向，已撤回）**：把"探测不了"降级成 warning 再继续，会让无法证伪的输入照常进 ASR；用 `has_audio / no_audio / probe_unknown` 三态对象或枚举承载，也属于为一个只有两个引用点的需求造类型。改为上面第 3、4 条：strict 准入 + 既有 `InvalidMediaError` 的具名消息。
- 把 `media_probe.py` 之类新模块当通用探测层：只有 `transcription.py` 一个消费者、两个引用点，函数就地放在该文件内即可。

### 增量二 · #147 有效 LLM 参数及来源诊断

**要点**

1. **形态**：`main.py` 在现有 `print("Configuration OK")` **之后**追加一行 JSON（`json.dumps(..., ensure_ascii=False, sort_keys=True)`，复用标准库）。现有断言 `"Configuration OK" in stdout` 的测试不受影响。
2. **取值唯一来源**：`main.py` 已从 `load_and_validate_config` 拿到校验后的 dict（`api/context.py:565-593` 返回 `validated`），再过一次 `LLMConfig.from_dict` 即得与启动路径**同一个解析器**的真实生效值。不写第二套手算默认值。
3. **白名单 9 键**及原始路径：`enable_threshold` / `segment_size` / `max_segment_size` ← `llm.segmentation.*`；`min_chunk_length` / `max_chunk_length` / `preferred_chunk_length` / `calibration_concurrent_limit` ← `llm.structured_calibration.*`；`structured_calibration_for_plain` ← `llm.*`；`structured_fallback_strategy` ← `llm.structured_calibration.quality_validation.fallback_strategy`。不为了凑数加第 10 个键。
4. **来源标签必须复刻 `from_dict` 的真实分支，不按"键在不在"编造**：
   - 前 8 个键在 `from_dict` 里都是裸 `dict.get(key, default)`（`llm/core/config.py:265-266`、`:277-282`、`:304-305`）。因此：键**存在**（哪怕值是 `null`）→ 来源 `config`，且诊断 `value` 必须与 `from_dict` 一致地原样为 `null`，因为 `.get` 会把显式 `None` 保留下来、**不会**回退到 default；键**不存在** → 来源 `default`，value 取 default。把显式 `null` 标成 `default` 同样是编造。
   - `structured_fallback_strategy` 是 9 个键里**唯一**走真值派生的（`llm/core/config.py:190-195`：`if not structured_fallback_strategy:`）。命中规则：`fallback_strategy` 为**缺失 / `None` / 空字符串 / 空容器**时进入派生，此时来源 `derived`，value 由 `llm.structured_calibration.fallback_to_original` 决定——缺失或为真值 → `formatted_original`，为假值（`False` / `0` / `""` / `None`）→ `best_quality`；`fallback_strategy` 有非空真值时来源 `config`，value 原样。
5. **函数落点**：诊断函数放 `main.py`（模块级私有函数），唯一消费者就是 `--check-config` 这一个 CLI 分支。**不在 `LLMConfig` 上新增接口**——真实服务进程不需要它，加了就是没有第二消费者的公开 API。若实现时确有放别处的理由，必须在 PR 里写明单消费者仍必要的具体理由；不新建 provenance 框架。
6. **凭据零泄漏**：白名单内没有任何 `api_key` / `base_url` / `*_model` / token / cookie / url；测试须断言输出文本不含配置文件里的 `api_key` 值。
7. **注释修正**：`config/config.example.jsonc:273` 的 `>=` 改为与 `plain_text_processor.py:121`（`len(text) > self.config.enable_threshold`）一致的措辞。**只改注释文字，不动任何数值。**

**已否决**

- `json.dumps` 整个配置：会打印 api_key / webhook / cookie。
- 新建通用"配置来源登记表"或标注框架：没有第二消费者，撞反过度设计红线。
- 统一 dataclass 默认 / 示例 / 生产三套取值：锁定决策否决；本单只观测不动。
- 顺带给 `validate_config` 加取值校验：属延后项，另开卡。

### 增量三 · #156 ASCII `!?` 局部切分

**要点**

1. **唯一改动点**：`llm/segmenters/dialog_segmenter.py:338` 的 `re.split(r'([。！？])', text)` → `([。！？!?])`。重组逻辑不动，**不补 `.`**。
2. **生效面**：`_split_by_sentences` 只被 `_split_long_dialog`（`:125`）调用，而 `_split_long_dialog` 在 `len(text) <= max_chunk_length` 时直接返回（`:120`）。因此本改动**只在超长 dialog 内**做句末切分——不承诺"所有英文句子都会被切成独立块"，短 dialog 的打包行为完全不变。
3. 硬上限兜底（`_split_long_dialog` → `split_oversized_text`）与 `max_chunk_length` 语义不动。
4. **既有空白契约保持不动**：`if sentence.strip()`（`:346`）丢弃纯空白尾片、`_split_long_dialog` 的 `.strip()`（`:131`/`:141`）剥掉每段首尾空白——这两条是现状契约，**本单不修**，也不把它们写成"缺陷待办"。
5. **断言分两层**：
   - `_split_by_sentences` 返回值：断言新增标点不丢（无空白尾片的样本 `''.join(result) == text` 成立）。
   - `_split_long_dialog` / `segment()`：只断言中文样本**逐项不变**、时间戳单调且首尾锚定。**不承诺全链路字符守恒**。
6. 真实入口是 `speaker_aware_processor.py:61`（有说话人）与 `:247`（plain 路径，用 `plain_structured_*` 覆盖长度参数）两条。**句子数量 ≠ LLM 调用次数**，调用数由 `segment()` 的打包层决定（一个 chunk 一次调用），测试不得拿"每句一次调用"当预期。

**已否决**

- 顺带补 `.`：会切碎英文缩写与小数点，且锁定决策明确只补 `!?`。
- 统一全仓 4 套句末定义（`capswriter_client` / `paragraphize` / `DialogSegmenter` / `TextSegmenter`）：#151/#152 暂缓决定不动，本单只改其中一处并在注释里更新对照表。

## 候选文件边界（供主脑拆实现卡）

| 增量 | 拟改文件 | 拟增/改测试 |
| --- | --- | --- |
| #159 | `src/video_transcript_api/api/services/transcription.py`（两个准入点 + 模块级私有探测函数 + 归因文案；`base.py` **不改**） | `tests/unit/test_transcription_audio_admission.py`（新）。**入口层断言**，不只测探测函数：两条共享入口各自跑通，断言任务终态 FAILED、失败通知 payload 含具名原因、`FunASRSpeakerClient.transcribe_sync` 与 CapsWriter 客户端**均未被调用**、临时文件经既有清理路径回收。真实 ffprobe CLI 跨进程用例保留**实际 argv**、**lavfi 动态生成的媒体**与**真实 ffprobe JSON** 输出，不用假 ffprobe 冒充全部边界；只有探测失败分支用隔离 mock。 |
| #147 | `main.py`（诊断函数 + `--check-config` 打印；`llm/core/config.py` 与 `base.py` 均**不改**）；`config/config.example.jsonc`（仅 `:273` 注释） | `tests/unit/test_llm_config_defaults.py`（扩来源标签用例：显式 `null`、缺失、`fallback_to_original=False`、`fallback_strategy=""` 四种）；`tests/unit/test_runtime_lifecycle.py` 邻侧新增 subprocess 用例（`env=` 显式最小集模拟无会话身份），断言 JSON 行可解析、含且仅含 9 键、不含配置文件 `api_key` 值 |
| #156 | `src/video_transcript_api/llm/segmenters/dialog_segmenter.py`（仅正则 + 注释对照表） | `tests/unit/test_dialog_segmenter_caps.py`（扩：ASCII `!?` 样本逐项断言；中文样本与 `test_punctuation_split_behaviour_is_unchanged` 邻证不变；`_split_long_dialog` 时间戳单调 + 首尾锚定） |

## 关键不变式

本节分两组：**A 组是今天已经成立的既有事实**（代码/现有测试已背书），**B 组是本批实现卡将要新建的回归保证**（现在尚不存在任何测试锁住它们，不得当作已验证）。

### A 组 · 现有事实（实现前即成立）

1. [实测] 无音轨纯视频**今天会被放行**：`base.py:417` 的 `not has_audio and not has_video`。本 worktree 用 lavfi 现场生成 `video_only.mp4` 实跑 `_validate_media_file`，日志为「文件验证通过: 音频流=False, 视频流=True」且写出 `duration=2.0`。仓内**没有任何**测试断言「无音轨纯视频被拒」——这正是缺口本身，不是已锁性质。
2. [实测] 归因断点在 `base.py:293-296`：`except InvalidMediaError` 直接 `return None`，异常消息永不外传；用户可见文本唯一汇流点是 `transcription.py:2287` 的 `下载文件失败: {url}`。
3. [实测] `actual_downloader` 在「已有本地文件」分支（`transcription.py:2251-2253`）未被赋值，`transcription.py:2296` 读到 `None` —— 准入判定不能依赖实例状态。
4. [实测] ffprobe 本地元数据探测（`-v quiet -print_format json -show_format -show_streams`）在本机对 10 KB mp4 耗时 0.02–0.03 s。
5. [实测] `main.py:27-31` 跑完全部启动期演练后只 `print("Configuration OK")`，不输出任何生效值。
6. [实测] 示例注释 `config/config.example.jsonc:273` 写 `>=`，实现 `plain_text_processor.py:121` 是严格大于。
7. [实测] `_split_by_sentences` 现状：`Is it ok? Yes! Great news! Really? Wow!` → 1 段；`今天很好。明天呢？后天啊！` → 3 段；`句一。\n\n。句二。` → 3 段且中间片 `'\n\n。'` 不丢；`第一句。   ` → 不守恒（`if sentence.strip()` 丢纯空白尾片）。
8. [实测·现有测试锁死] `tests/unit/test_llm_config_defaults.py` 锁死 `from_dict` 与 dataclass 直构逐字段一致（#158）——`#147` 的取值来源必须继续走 `from_dict`，不得旁路。
9. [实测·现有测试锁死] `tests/unit/test_runtime_lifecycle.py::test_check_config_is_side_effect_free` 已用 subprocess 跑 `main.py --check-config`，断言 stdout 含 `Configuration OK` 且不产生副作用目录。
10. [实测·现有测试锁死] `tests/unit/test_dialog_segmenter_caps.py::test_punctuation_split_behaviour_is_unchanged` 只用 `。`，断言结果与切分数量——加 `!?` 不影响它，可作邻证。

### B 组 · 本批将新建的回归保证（当前尚不存在）

11. [将新增] 无音轨媒体在**两条共享入口**都被拒，终态 FAILED、通知含 `no_audio_track` 原因、两个引擎零调用、文件清理照旧。锁在 `tests/unit/test_transcription_audio_admission.py`。
12. [将新增] 探测失败（ffprobe 缺失 / 超时 / 非零退出 / 非法 JSON）与「确实无音轨」产生**两种不同的失败文案**，前者不得被表述为"不含音轨"。同上测试文件，失败分支用隔离 mock。
13. [将新增] `--check-config` 输出首行仍是 `Configuration OK`，末行 JSON 含且仅含 9 个白名单键，各带 `value` + `source`；`source` 与 `from_dict` 的真实分支一致（显式 `null` 标 `config` 且 value 为 `null`；缺失标 `default`；`structured_fallback_strategy` 走真值分支时标 `derived`）。锁在 `main.py` 侧测试。
14. [将新增] 含 ASCII `!?` 的超长 dialog 被切成多块、时间戳单调且首尾锚定；中文样本输出与改动前逐项相同。锁在 `tests/unit/test_dialog_segmenter_caps.py`。

## 待验证前提

1. [推断] ASR 入口只有 `transcription.py` 这两处：需在实现卡里用 `rg -n "transcribe_sync|CapsWriterClient\(|funasr" src/` 复核有无第三条直调入口（API handler 或其他 service）。若存在第三条，探测函数的引用点从 2 变 3，设计其余部分不变。
2. [推断] 30 MB 级真实媒体 ffprobe 仍在百毫秒内：本次只量了 10 KB 样本，未取真实媒体。超预算会直接拉长每个任务的关键路径。
3. [推断] `youtube-api` 分支的 `audio_path` 必为音频文件：在 strict 准入下，若该值偶发是纯视频，该分支的任务会从"转录出空结果"变成"明确失败"——这是期望方向，但该值由外部 API Server 产出，本仓无法静态证明。
4. [推断] `_split_by_sentences` 只在 `_split_long_dialog`（`:125`）被调用，故本改动的实际生效面仅限超长单条对话。
5. [推断] CI 具备 `ffmpeg` / `ffprobe`：真实 CLI 用例依赖这两个二进制。若 CI 缺，**照实上报**（用例 xfail/skip 需在 PR 里写明原因与本地实跑证据），**不得**为了让它变绿去改 workflow —— 那是越权兜底。

## 验收路径

1. **入口**：#159 —— 提交一个无音轨纯视频 URL 的转录请求（API 层，与生产同入口）。
   **步骤**：任务进入下载 → 准入点 → ASR。
   **预期**：
   - 无音轨样本：任务终态 FAILED；失败通知 payload 含「不含音轨」及具名原因 `no_audio_track`，而不是笼统的「下载文件失败」；`FunASRSpeakerClient.transcribe_sync` 与 CapsWriter 客户端**均未被调用**；临时文件经既有清理路径回收。
   - 探测失败样本（ffprobe 缺失 / 超时 / 非零退出 / 非法 JSON）：同样 FAILED，但文案是「无法校验媒体音轨（检查失败）」且原因标 `media_probe_failed`，**不得**出现「不含音轨」字样；引擎同样零调用。
   - 有声样本与音视频混合样本：正常放行进入 ASR（混合文件不抽音轨）。
   - 两条共享入口（常规路径与 `youtube-api` 路径）**各自**跑通以上断言，不只测探测函数本身。
2. **入口**：#147 —— 在**无会话身份的环境**跑 `env -i PATH=/usr/bin:/bin <python> main.py --check-config --config <path>`。
   **步骤**：读 stdout 最后一行 JSON。
   **预期**：首行仍是 `Configuration OK`；JSON 行含且仅含 9 个白名单键，各带 `value` 与 `source`；输出中不出现配置文件里的 `api_key` 值；四种来源情形各自落对标签——键显式给值或显式 `null` → `config`（`value` 原样，含 `null`）；键缺失 → `default`；`fallback_strategy` 空/缺失且 `fallback_to_original` 为假 → `derived` + `best_quality`；`fallback_to_original` 缺失或为真 → `derived` + `formatted_original`。
3. **入口**：#156 —— plain 与 dialog 两条完整分块路径（`speaker_aware_processor.py:61` 与 `:247`），输入含 ASCII `!?` 的**超长**文本。
   **步骤**：走 `segment()` → `_split_long_dialog` → `_split_by_sentences`。
   **预期**：英文超长样本被切成多块且时间戳单调、首尾等于原 dialog 的起止；中文样本输出与改动前逐项相同；短 dialog（未超 `max_chunk_length`）的打包结果不变；现有 `test_punctuation_split_behaviour_is_unchanged` 仍绿。不对「短文本里的每个英文句子都被单独成块」设预期。
