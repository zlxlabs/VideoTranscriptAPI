# 处理深度开关（processing_options）

## 功能概述

`POST /api/transcribe` 支持 `processing_options` 参数，按任务控制处理深度：只转录、转录+校对、总结、章节梗概等。配合分层缓存，重复提交同一视频只会补跑"缺失且被请求"的层，不会重新下载或重新转录。

## API 参数说明

### 请求格式

```json
{
  "url": "https://www.youtube.com/watch?v=abc123",
  "use_speaker_recognition": true,
  "processing_options": {
    "calibrate": true,
    "summarize": true,
    "infer_speaker_names": true,
    "chapters": true
  }
}
```

### 参数说明

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| `processing_options` | object | 否 | `null` | 处理深度开关，`null` 等价于全部启用（历史行为） |
| `processing_options.calibrate` | boolean | 否 | `true` | 是否执行 LLM 校对 |
| `processing_options.summarize` | boolean | 否 | `true` | 是否生成内容总结 |
| `processing_options.infer_speaker_names` | boolean | 否 | `true` | 是否把说话人占位标签推断为真实姓名；仅在启用说话人识别时生效 |
| `processing_options.chapters` | boolean \| 省略 | 否 | **跟随 `summarize`** | 是否生成章节梗概。请求体**未出现**该字段时，规范化后等于生效的 `summarize` 值；显式 `true`/`false` 原样保留。**禁止**理解为「默认恒 true」——老客户端 `{calibrate:false, summarize:false}` 不会意外触发章节 |

唯一的规范化模型定义在 `src/video_transcript_api/api/processing_options.py::ProcessingOptions`。`calibrate` / `summarize` / `infer_speaker_names` 只接受 JSON boolean；`chapters` 可省略（模型字段为 `null`），由 `normalize_processing_options()` 填成跟随 summarize 后的布尔值。未知或拼错的字段会直接返回校验错误，不会静默忽略后按默认值启用功能。

章节层的分层满足判定是**状态敏感**的（`generated`+文件 / `skipped_*` 满足；`failed`/`disabled`/不一致可重跑），详见 [chapters.md](./chapters.md)。仅 `chapters=true` **不会**单独触发 LLM 标题生成。

### 开关组合语义

| calibrate | summarize | 行为 |
|:---:|:---:|------|
| `true` | `true` | 完整流程：转录 → 校对 → 总结（默认行为，等价于不传 `processing_options`） |
| `true` | `false` | 只转录 + 校对，不生成总结 |
| `false` | `true` | 只转录 + 总结，**总结基于未校对的原始转录文本生成**，质量可能受 ASR 识别噪声（错别字、断句错误等）影响，但仍可用；系统不做硬性拦截，由调用方自行权衡 |
| `false` | `false` | 只转录，不校对也不总结 |

> **独立说话人开关**：`infer_speaker_names` 不依赖 `calibrate` 或 `summarize`。因此只关闭后两者但保留 `infer_speaker_names=true` 时，有说话人识别数据的任务仍可调用 LLM；没有说话人识别数据时该开关不生效，也不会仅为生成标题而调用 LLM。三个开关全部为 `false` 始终是明确的零 LLM 请求语义。

说话人姓名映射以版本化 `speaker_mapping.json` artifact 保存到 `video_cache.files_loc` 指向的媒体目录。artifact 包含转录/diarization 输入指纹、完整 speaker 集合和 `source=llm`；写入复用媒体锁并通过临时文件原子替换。`infer_speaker_names=false` 时只复用指纹与 speaker 集合均有效的完整映射，未命中则保留原始通用标签，且不会把通用标签写成“已完成”缓存。

## 分层缓存复用

### 核心原则：产物只增不减

一个视频（`platform` + `media_id` + `use_speaker_recognition` 三元组）的缓存里，`transcript`（转录）/ `calibrated`（校对）/ `summary`（总结）三层产物只会累加，不会因为某次请求关闭某个开关而被清空或覆盖。

### 命中判定逻辑

命中判定发生在 `process_transcription()`（`src/video_transcript_api/api/services/transcription.py`）的缓存检测分支：

```
calibrated_layer_satisfied = 已有 llm_calibrated 文件 AND 该文件的 calibration_status != DISABLED/NONE 且 status 已确认
need_calibrated = 本次请求 calibrate=True AND NOT calibrated_layer_satisfied
need_summary    = 本次请求 summarize=True AND NOT 已有 llm_summary 文件
need_chapters   = 本次请求 chapters=True AND NOT chapters_layer_satisfied
  其中 chapters_layer_satisfied：
    - chapters_status==generated 且 llm_chapters.json 存在 → 是
    - chapters_status in {skipped_short, skipped_no_timeline} → 是
    - failed / disabled / 缺省 / 仅文件或仅状态 → 否

若 need_calibrated、need_summary、need_chapters（及 need_speaker_names）都为 False -> 全命中，直接返回
否则 -> 部分命中，只把需要的层重新入队
```

`calibrated` 层的"已满足"判定不能只看文件是否存在——如果上一轮请求 `calibrate=false`，`llm_calibrated.txt` 仍会被写入（内容是本地格式化的原文，`calibration_status=disabled`），此时若本轮请求 `calibrate=true`，必须视为"缺失"以触发真实校对，而不是把 disabled 占位文本当成已完成的校对结果直接返回。`summary` 层：`disabled`/`failed` 都不落盘 `llm_summary.txt`，因此文件存在即代表该层已有确定性产物。`chapters` 层**不要**抄 summary 的「有文件即满足」——必须看 `chapters_status`（见上表与 [chapters.md](./chapters.md)）。

### 命中矩阵

以下矩阵描述"先提交 A 请求、缓存产物落盘后，再提交 B 请求"时的实际行为（`tests/integration/test_layered_cache.py::TestLayeredCacheMatrix` 覆盖）：

| 场景 | 第一次请求 | 第二次请求 | 判定 | 第二次实际处理 |
|------|-----------|-----------|------|----------------|
| 1 | `{calibrate:true, summarize:true}`（完整流程） | `{calibrate:false, summarize:false}`（只要转录） | 全命中 | 无 LLM 任务入队，直接返回已有的校对+总结 |
| 2 | `{calibrate:false, summarize:false}`（只转录） | `{calibrate:true, summarize:true}`（完整流程） | 部分命中 | 重新入队 `{calibrate:true, summarize:true}`，用**原始转录**（非 disabled 占位文本）作为输入 |
| 3 | `{calibrate:true, summarize:false}`（只校对） | `{calibrate:true, summarize:true}`（完整流程） | 部分命中 | 只入队 `{calibrate:false, summarize:true}`；总结阶段复用**已有的真实校对文本**作为输入（而非原始转录），且强制走纯文本路径，不重复说话人推断 |
| 4 | `{calibrate:true, summarize:true}` | 相同的 `{calibrate:true, summarize:true}` | 全命中（幂等） | 重复提交完全相同的开关组合，两次都直接返回，不重复处理 |
| 5（历史兼容） | 不传 `processing_options`（等价全 True） | — | 按 `has_llm_calibrated`/`has_llm_summary` 是否存在判定，行为与功能上线前完全一致 |

### `recalibrate` 是唯一的强制重做例外

`POST /api/recalibrate` 端点（重新校对）从不设置 `processing_options`，因此天然落到 `{calibrate:true, summarize:true}` 默认值；同时它携带 `calibrate_only=True` 标记，会绕过"已存在层不覆盖"的抑制逻辑（`llm_ops._save_llm_results` 中的 `suppress_calibration` 判断），无论校对层是否已存在都会真实重跑一次校对。这是分层缓存"只增不减"原则下唯一允许强制覆盖已有校对产物的入口。

`recalibrate` 若发现缓存里 `llm_summary.txt` 缺失或为空（老任务遗留），会顺手补跑一次总结（`_should_backfill_summary`），避免停留在总结永久 pending 的状态；其余情况总结层保持不动。

## disabled 状态的表现

用户通过 `processing_options` 主动关闭某一层时，落盘状态与 `NONE`/`FAILED`（尝试但失败）明确区分，属于诚实状态模型的一部分（详见 [状态模型说明](../architecture.md#诚实状态模型)）：

- **校对被关闭**：`calibration_status = disabled`。该状态**只在首次关闭时**（缓存目录尚无校对文件）被记录并落盘一份本地格式化的原始转录作为占位；若某层此前已有真实产物，本轮未请求该层时保留旧值不变，不会被抹成 disabled。
- **总结被关闭**：`summary_status = disabled`。同样只在首次关闭（缓存目录尚无 `llm_summary.txt`）时记为 disabled；不落盘任何总结文件。

### 页面表现（`/view/{view_token}`，`transcript.html`）

- 校对区：当 `stats.calibration_status == 'disabled'` 时，展示提示条"本任务未启用 AI 校对，以下为原始转录"，正文展示原始转录（不冒充校对结果）。
- 总结区四态文案：

| `summary_state` | 展示文案 |
|---|---|
| `pending`（或旧数据缺字段） | "总结处理中..." |
| `skipped_short` | "该任务未生成总结（原始文本过短，未达到生成总结的长度阈值）。" |
| `failed` | "⚠️ 总结生成失败，可点击下方「重新校对」按钮重试。" |
| `disabled` | "该任务未启用内容总结。" |

### 通知文案（企微/飞书）

`_send_notification`（`src/video_transcript_api/api/services/llm_ops.py`）里的总结状态标签：

| `summary_status` | 通知文案 |
|---|---|
| `failed` | "生成失败" |
| `disabled` | "未启用" |
| 其他（含缺失、pending） | "未生成" |

### API 响应

- `GET /api/audit/history`：每条记录携带 `calibration_status`/`summary_status` 原始取值（可能为 `disabled`），供前端后续迭代消费。
- `GET /api/audit/summary`：`summary_status` 为非 `generated` 时（含 `disabled`），`data.summary` 恒为 `null`，不再返回"总结处理中..."之类的占位字符串。

## 相关文件

| 文件 | 说明 |
|------|------|
| `src/video_transcript_api/api/processing_options.py` | 唯一的 `ProcessingOptions` 模型与规范化函数 |
| `src/video_transcript_api/api/services/transcription.py` | 分层缓存命中判定与规范化选项透传 |
| `src/video_transcript_api/api/services/llm_ops.py` | `_save_llm_results()` 分层落盘保护逻辑、通知文案 |
| `src/video_transcript_api/utils/llm_status.py` | `CalibrationStatus`/`SummaryStatus` 枚举定义 |
| `src/video_transcript_api/cache/cache_manager.py` | `save_llm_status()` 合并、说话人 artifact 的 `files_loc` 定位与原子写入 |
| `src/web/templates/transcript.html` | 校对警告条 + 总结四态文案渲染 |
| `tests/integration/test_layered_cache.py` | 分层缓存命中矩阵集成测试 |
| `tests/unit/test_processing_options.py` | 请求 schema 与任务字典透传单测 |
