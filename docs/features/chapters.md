# 章节梗概（Chapters）

## 功能概述

对长逐字稿生成结构化章节列表：每章含标题（title）、梗概（gist）与时间范围。章节**只依赖 timeline 数据层**（segments / dialogs），不依赖校对是否成功。

默认行为：请求未显式指定 `processing_options.chapters` 时，**跟随 `summarize` 的生效值**。老客户端关闭 summarize 不会意外触发章节付费调用。

## 产物契约

| 项 | 说明 |
|----|------|
| 文件 | 缓存目录下 `llm_chapters.json` |
| 状态 | `llm_status.json` 的 `chapters_status`（及 `task_status.chapters_status`） |
| 仅 GENERATED 写文件 | `skipped_short` / `skipped_no_timeline` / `failed` / `disabled` 只写状态、不写章节文件 |

### `llm_chapters.json` 结构

```json
{
  "format_version": "v1",
  "source": {
    "kind": "dialogs|segments|...",
    "segment_count": 120,
    "fingerprint": "<sha1>",
    "generated_at": "ISO8601"
  },
  "chapters": [
    {
      "index": 0,
      "title": "开场",
      "gist": "两到三句梗概",
      "start_seg": 0,
      "end_seg": 15,
      "start_time": 0.0,
      "end_time": 92.5
    }
  ]
}
```

- `start_seg` / `end_seg` 是**原始输入列表下标**（闭区间），对应页面锚点 `#dlg-{start_seg}`。  
- 时间由本地从 segments 反查，不信任 LLM 抄写的时间戳。  
- `fingerprint` 覆盖锚点源文本与时间轴；与当前页数据不一致时，前端仍展示章节卡片但**去掉跳转链接**。

## 状态模型（`ChaptersStatus`）

| 值 | 含义 | 分层是否满足（不必重跑） |
|----|------|--------------------------|
| `generated` | 成功且文件存在 | 是（需同时有文件） |
| `skipped_short` | 原文过短 | 是 |
| `skipped_no_timeline` | 无可用 timeline（含有效段 &lt; 2） | 是 |
| `failed` | 触发了但失败 | 否（可重跑） |
| `disabled` | 用户关闭 | 否（仅当本轮 `chapters=true` 时再生成） |
| 缺省 / 文件与状态不一致 | — | 否（保守重跑） |

## 管线要点

1. **输入梯度**：本轮 structured dialogs → 缓存 `llm_processed.json` dialogs → `load_segments()` 原始 segments → 都无则 `skipped_no_timeline`。  
2. **落盘**：与其它 LLM 产物同一 `media_lock`；写产物前 write-ahead `invalidate_llm_status`。  
3. **suppress**：已有 GENERATED 且本轮未请求 chapters → 不覆盖。  
4. **recalibrate**：原 `chapters_status=generated` 时**强制联动重算**（与 summary 的「仅缺失 backfill」不同）。  
5. **title 副作用**：仅 `chapters=true` **不会**单独触发 LLM 标题生成。

## 前端展示

- 仅 `chapters_status=generated` 且存在 `llm_chapters.json` 时渲染章节区块。  
- `title` / `gist` 一律 `html.escape`；TOC 使用 DOM API + `textContent`（禁止 `insertAdjacentHTML` 拼接用户/章节标题）。  
- 结构化 dialog 带 `id="dlg-{i}"`；章节标题在 fingerprint 一致时跳转 `#dlg-{start_seg}`。

## 相关代码

| 层 | 路径 |
|----|------|
| Options | `api/processing_options.py` |
| 分层入队 | `api/services/transcription.py`（`need_chapters`） |
| 协调器 | `llm/coordinator.py`（`stage="chapters"`） |
| 处理器 | `llm/processors/chapters_processor.py` |
| 落盘 | `api/services/llm_ops.py`、`cache/cache_manager.py` |
| 视图 | `api/routes/views.py`、`utils/rendering/dialog_renderer.py` |
| TOC | `web/static/js/floating-toc.js` |

## 配置

见 `config` 中 `llm.chapters_model` / `chapters_reasoning_effort` / `min_chapters_threshold` / `max_chapters_input_chars`（须与模型上下文能力匹配）。

## 未做 / 后续

- 阶段二：`structured_calibration_for_plain` 推广（T8，独立开关）  
- 真实长样本质量验收与 prompt 定稿（T9）  
- 无 structured 锚点时的 jump 门控收紧（session backlog B7）  
