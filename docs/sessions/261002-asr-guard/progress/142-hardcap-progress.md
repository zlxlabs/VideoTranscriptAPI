# #142 段长上限不可放弃兜底 —— 进度存档

## ① 读设计单 + 摸清两处执法点现状

- **当前阶段**：repairing（第 1/4 段：现状核查完成，尚未改生产代码）
- **本段结论**：两个执法点的放弃路径都已定位并实测复现——`capswriter_client._split_long_segment` 在
  `len(parts) <= 1`（无 `，,；;`）时 `return [segment]` 且不打日志，`min_len=80, max_len=300`
  下 `'词'*800` 产出 1 段 800 字；`DialogSegmenter._split_long_dialog` 的「超长单句整条进一个子 dialog」
  无兜底，`max_chunk_length=1500` 下 2159 字无逗号英文独白产出 1 个 2159 字 chunk。
  `git log -S "if len(parts) <= 1"` 的 introduced_by_commit = `b8f2bcc5`（存量，早于 `c6e489f4`）。
- **关键决策与已否决方案**：
  - 兜底形态按锁定决策 1：逗号/句末切完后仍超上限的片段，空白优先切、无空白按上限硬切，时间沿用
    现有插值器（`interpolate_segment_times` / `_interpolate_dialog_times`），不新写插值。
  - 已否决（沿用设计单）：统一校验/降级框架、新建计数告警机制、给 `primary_punct` 加 ASCII `.`、
    改 `paragraphize` 行为、改 `LLMConfig` 默认值、改 `.strip()` 语义。
  - 共享 helper 不落在 `transcriber/segments.py`：该文件不在本卡 Scope-Globs 内，兜底在两个生产者里
    各写一份小函数（重复约 12 行，换取改动不越界）。
- **下一步唯一动作**：实现 `_split_long_segment` 的兜底并在 `test_cache_timeline_wiring.py` 追加形态②③用例。
