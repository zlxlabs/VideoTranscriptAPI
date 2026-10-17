<!-- delegate-outcome: succeeded -->

## Verdict

- 判定：发现 1 条 P2；不阻断交付。
- 严重度：P2。触发需要 CapsWriter 已成功写出正文文件后，Transcriber 读取该文件发生 I/O 错误；个人单机部署中触发面窄，但一旦发生会把空正文作为成功结果继续处理。
- 交付阻断：否。按 personal 风险等级，本轮空 producer 结果、空白正文和字幕边界均已在最窄写入点拒绝；遗留项只覆盖文件读取故障。
- failure-visibility: p2-only

## 审查范围

固定审查 `29f24581a8532605413c0aab22faec028deb5e58..250e8e96d80111a0013246e50a9355675d0a2a14`，风险等级 personal。核对被审设计文档、三个生产文件、缓存写入与 LLM handoff 的直接调用方，以及新增集成用例。没有读取实现方报告，也没有修改产品、测试或配置。

OCR 前置扫描状态为 `reviewed`，主腿返回 1 条候选；下列 P2 经源码核对确认。OCR 仅作初筛，结论按本仓不变式裁决。

## 不变式核查

1. **FunASR 空 segments/空白文本：通过。** `transcribe_sync` 在真实 formatter 返回后检查格式化正文；空串或纯空白会抛 `EmptyTranscriptError`（`funasr_client.py:797-800`）。主路径和 YouTube API 音频路径分别在 `transcription.py:2518-2527`、`2101-2107` 调用它，异常发生在对应缓存写入和后续 handoff 之前。新增用例覆盖空 segments、空白 segment、两个入口以及数字正文（`test_issue243_empty_transcript.py:94-141`）。
2. **CapsWriter 空白 token：部分通过，见 P2。** 正常读取成功时，Transcriber 对 `.txt` 正文 `.strip()` 后拒绝空白（`transcriber.py:157-164`）；新增用例实际经过 CapsWriter `_save_results` 写文件，再由 Transcriber 读到空格并失败（`test_issue243_empty_transcript.py:191-212`）。未改 token/timestamp 校验契约。读取本身异常时检查未执行，详见 P2。
3. **YouTube API 平台字幕：通过。** 空正文有可用 segments 时先经 `_rebuild_text_from_segments` 恢复；恢复后仍空则调用既有失败收口，并在缓存写入前返回（`transcription.py:1973-1986`）。新增用例调用真实 SRT parser，覆盖空字幕失败和纯数字 cue 恢复（`test_issue243_empty_transcript.py:157-188`）。
4. **合法数字正文与其他缓存变体：通过。** 数字 FunASR 正文经 formatter、缓存并交给 LLM handoff；空新结果在 `save_cache` 之前失败，不覆盖另一变体的有效缓存（新增用例：`test_issue243_empty_transcript.py:124-154`）。
5. **历史读取契约和范围：通过。** 本次 diff 未改 formatter、CacheManager reader 或 export 路径，也未增加新失败状态、重试或兜底框架；只在 producer/Transcriber/字幕消费边界拒绝空正文。

## Findings

### P2 — CapsWriter 正文读取失败会降级为空正文成功

- 违反：目标总述“空转写不会被写入成功缓存或交给下游大模型”，以及不变式 2 的 Transcriber 正文失败边界。此触发源是读取异常，不是正常读取到空格 token。
- 位置：`src/video_transcript_api/transcriber/transcriber.py:160-164`。读取失败分支只记 warning；`result["transcript"]` 初始值仍为空串，而 `txt_path` 已在读取前设定。新增的空白检查只在 `else`（读取成功）分支执行；函数后续只确认 `txt_path` 存在便返回（`:175-181`）。
- 触发方式：CapsWriter 已返回 `.txt` 路径且写入成功后，令 `open()` 或 `read()` 因文件 I/O 错误而抛异常；Transcriber 捕获异常后返回 `transcript == ""`。调用方随后以该值调用 `save_cache`（`api/services/transcription.py:2575-2596`），再把它放入 LLM handoff payload（`:2636-2651`）。
- 风险：读取故障会留下 warning 日志，但任务仍可能成功缓存空正文并向下游交付空文本。
- 处置：P2，不阻断交付。正常 producer 空白结果已被本次改动拒绝；读取异常属于低频 I/O 路径。若后续修复，应让读取失败上抛或在读取循环后统一校验最终正文，避免读取错误被降级为空成功。

## 验证与现场

- `git diff --check 29f24581a8532605413c0aab22faec028deb5e58 250e8e96d80111a0013246e50a9355675d0a2a14`：通过。
- 检查约束力：临时在本 verdict 标题行注入尾随空格后，`git diff --check` 以退出码 2 报出该行；复原后未暂存与暂存 diff 检查均退出码 0。
- 本卡未运行 pytest；指定验证命令仅为 `git diff --check`。
- CI 未运行。派发基线 API 不可用，因此继承红未能判定；本次没有新增 CI 红的结论。
- 接手锚点：无交接单。pickup 巡检原文：

  ## 巡检
  open issue 总数：4
  存活探针不可用
  memory 候审 1 条（死指针 0 / 超龄 0 / 探针失败 1 / 探针出错 0），详单在本机状态目录，时间 2026-10-12T01:04:06Z

- 全局待处理清单中，VideoTranscriptAPI #263 等待评审且已有负责人；同 dispatch id 的 unit/pane/报告目录属于本卡，不作为工作树占用。
