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
## ② `_split_long_segment` 兜底 + 测试

- **当前阶段**：repairing（第 2/4 段完成）
- **本段结论**：删掉 `len(parts) <= 1: return [segment]` 的放弃路径，改为逗号切完后对仍超
  `max_len` 的片段走兜底（`_split_oversized_text`：窗口内空白串结尾优先、否则按 `max_len` 硬切），
  超上限时打 warning（orig_length / parts / max_len / whitespace_cuts / hard_cuts）。复现命令 1
  从 `1 800 True` 翻转为 `3 300 True`，正文逐字一致、时间不倒挂。
- **关键决策与已否决方案**：
  - warning 在「兜底触发」时打（不只在硬切时），因为形态②的英文长段同样意味着上限不是靠标点
    兑现的；消息里用 `whitespace_cuts` / `hard_cuts` 区分形态。
  - 兜底 helper 放在 `capswriter_client.py` 模块内而不是 `transcriber/segments.py`：后者不在本卡
    Scope-Globs。
  - 未新增配置项/开关/阈值调整；`max_len=300` 与 `min_len=80` 原值不动。
  - 现状（不新增分支）：超长纯空白段此前因「无标点放弃」而保留 None 时间，兜底后会走插值器拿到
    时间；这是超长空白段这一病理形态下的既有插值口径，不单开特例。
- **下一步唯一动作**：实现 `_split_long_dialog` 的同形兜底并新建 `tests/unit/test_dialog_segmenter_caps.py`。

## ③ `_split_long_dialog` 兜底 + 测试

- **当前阶段**：repairing（第 3/4 段完成）
- **本段结论**：`_split_long_dialog` 在句末切分后对仍超 `max_chunk_length` 的子 dialog 追加同形兜底
  （空白优先 / 无空白硬切），碎片沿用 `dialog.copy()` 且不改 id，时间仍交给
  `_interpolate_dialog_times`；复现命令 2 从 `2159 False` 翻转为 `1494 True` + `665 True`。
  新用例放在新建的 `tests/unit/test_dialog_segmenter_caps.py`（含形态②英文无标点、形态③无空白硬切、
  id 与时间轴、非有限时间降级、标点路径不变共 6 条）。
- **关键决策与已否决方案**：
  - 兜底不额外 strip：分片可能以空白开头/结尾，既有 `.strip()` 语义保持不动（去空白后一致即通过）。
  - warning 措辞用英文（模块内既有 debug 日志是英文），logger 为本模块 `setup_logger(__name__)`。
  - 两处 helper 在各自生产者内各写一份（不跨包共用）：`transcriber/segments.py` 不在本卡
    Scope-Globs，且上限语义不同，不需要合并。
- **下一步唯一动作**：两条链各做一次反向红验（只关兜底判据），随后改两处「硬上限」文案并跑
  `make test`。


