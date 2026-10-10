# 独立 Review Verdict：章节排序归一化与 chapters_error 落盘

审查对象：`30466452..819b6f8435d191ba3fad9734f5d757d28295521e`（冻结 H0）
风险档位：personal
结论：**继续**（p2-only，不阻断交付）
failure-visibility: p2-only

## Findings

### P2 — 两个单调用统计 helper 没有第二消费者

- 位置：`src/video_transcript_api/llm/processors/chapters_processor.py:631`、`src/video_transcript_api/llm/processors/chapters_processor.py:642`、调用点 `src/video_transcript_api/llm/processors/chapters_processor.py:787`。
- 违反：卡面反熵条款要求对新增符号检查第二消费者；全局约束也要求新增 helper 有已发生的失败或第二调用方。`rg` 全仓结果显示 `_count_moved_entries` 与 `_longest_increasing_subsequence` 各只有该归一化分支一个生产调用点，未发现既有失败需要这层抽象。
- 影响：不影响归一化结果或 warning 数值；增加两个仅服务一个分支的符号与维护面。作为低风险清理项，不阻断交付；可接受不修，若修则可把统计内联到该分支。

## P1 判定

无 P1 finding，因此无需执行 P1 两问。唯一 finding 为 P2，personal 风险下不阻断交付。

## I1–I10 不变式核对

- **I1 通过**：归一化排序在 `src/video_transcript_api/llm/processors/chapters_processor.py:795`，区间推导在 `src/video_transcript_api/llm/processors/chapters_processor.py:1328`；生产样本测试还断言排序后区间连续且末章覆盖最后下标（`tests/unit/test_chapters_processor.py:1040`）。
- **I2 通过**：排序移动完整字典记录，仅以 `start_seg` 为键（`src/video_transcript_api/llm/processors/chapters_processor.py:779`、`src/video_transcript_api/llm/processors/chapters_processor.py:795`）；生产样本测试比较完整 `(title, gist, start_seg)` 集合（`tests/unit/test_chapters_processor.py:1033`）。
- **I3 通过**：仅 `is_sorted` 为假时计算统计、告警并排序（`src/video_transcript_api/llm/processors/chapters_processor.py:782`）；已排序输入测试断言顺序不变、只调用一次且没有排序 warning（`tests/unit/test_chapters_processor.py:1077`）。
- **I4 通过**：乱序 warning 同时记录 moved 数与 LIS 长度（`src/video_transcript_api/llm/processors/chapters_processor.py:789`）；生产序列测试断言 `moved 6/17`、`LIS 14/17`（`tests/unit/test_chapters_processor.py:1056`）。
- **I5 通过**：幸存下标成员校验仍在排序前执行（`src/video_transcript_api/llm/processors/chapters_processor.py:742`）；去重保留首次（`src/video_transcript_api/llm/processors/chapters_processor.py:771`）；title/gist 非空校验（`src/video_transcript_api/llm/processors/chapters_processor.py:749`、`src/video_transcript_api/llm/processors/chapters_processor.py:756`）、首章钳位（`src/video_transcript_api/llm/processors/chapters_processor.py:799`）和章节数量边界（`src/video_transcript_api/llm/processors/chapters_processor.py:1314`）均保留。测试继续覆盖越界、重复、钳位与空 title/gist（`tests/unit/test_chapters_processor.py:890`、`tests/unit/test_chapters_processor.py:970`、`tests/unit/test_chapters_processor.py:1120`）。
- **I6 通过**：指纹仍基于输入 segments 而非章节数组顺序（`src/video_transcript_api/llm/processors/chapters_processor.py:283`、`src/video_transcript_api/llm/processors/chapters_processor.py:1133`）；排序后仍先推导 end/time，再合并相邻同名章（`src/video_transcript_api/llm/processors/chapters_processor.py:1328`、`src/video_transcript_api/llm/processors/chapters_processor.py:1332`）。内嵌章节头按 `start_seg` 对齐原始 dialog 下标（`src/video_transcript_api/utils/rendering/dialog_renderer.py:439`、`src/video_transcript_api/utils/rendering/dialog_renderer.py:525`）；笔记切片按闭区间下标取原始 segments（`src/video_transcript_api/llm/processors/notes_processor.py:188`、`src/video_transcript_api/llm/processors/notes_processor.py:272`）。生产样本的配对与区间测试通过。
- **I7 通过**：Coordinator 把 `ChaptersResult.error` 放入 stats（`src/video_transcript_api/llm/coordinator.py:332`、`src/video_transcript_api/llm/coordinator.py:388`）；llm_ops 顶层优先、stats 兜底后传给状态写入（`src/video_transcript_api/api/services/llm_ops.py:1990`、`src/video_transcript_api/api/services/llm_ops.py:2489`）；CacheManager 将非 None 值写入合并状态（`src/video_transcript_api/cache/cache_manager.py:2062`）。测试读取实际写出的 `llm_status.json` 并断言失败状态与错误字段（`tests/unit/test_cache_manager.py:1797`）。
- **I8 通过**：CacheManager 在写入 error 后按 `chapters_status == "generated"` 强制删除该字段（`src/video_transcript_api/cache/cache_manager.py:2064`、`src/video_transcript_api/cache/cache_manager.py:2070`）；测试覆盖 FAILED 补跑清除及 caller 误传仍清除（`tests/unit/test_cache_manager.py:1810`、`tests/unit/test_cache_manager.py:1824`）。
- **I9 通过**：未触碰章节层时 `chapters_status` 与 `chapters_error` 都从撤销前快照回填（`src/video_transcript_api/api/services/llm_ops.py:2396`、`src/video_transcript_api/api/services/llm_ops.py:2410`）；resummarize 接线测试断言两者均保留（`tests/unit/test_chapters_pipeline_wiring.py:282`）。generate_notes 只更新 `notes_status`（`src/video_transcript_api/api/services/llm_ops.py:473`），CacheManager 的 None 合并语义保留旧 error，且有实际文件断言（`tests/unit/test_cache_manager.py:1842`）。
- **I10 通过**：字段仍只在非 None 时更新，并且读改写全程处于 `media_lock` 内（`src/video_transcript_api/cache/cache_manager.py:2031`、`src/video_transcript_api/cache/cache_manager.py:2054`）；未触碰测试验证旧状态与 error 均保留（`tests/unit/test_cache_manager.py:1842`）。

## 必查项与验证

- 测试断言抽查：新增排序测试用具体的序列、配对、区间、warning 数值断言；落盘测试读取实际 JSON。未发现恒真断言。
- 静默失败候选扫描：新增生产行未命中吞异常、异常后返回空值/成功等候选；扫描唯一命中 `if not is_sorted` 是正常排序分支。
- 新符号检查：两个统计 helper 只有一个生产调用方；已作为上方 P2 记录。
- `git diff --check 30466452..819b6f84`：通过，退出码 0。
- `uv run pytest tests/unit/test_chapters_pipeline_wiring.py tests/unit/test_cache_manager.py tests/unit/test_chapters_processor.py -p no:warnings`：`261 passed in 2.68s`。
- 主干 CI 基线：卡面记录 `gh api request failed`；继承红未能判定。本轮本地 Narrow-Verify 未观察到新红。
- OCR 前置扫描：`skipped`。OCR 工具可用，但卡面禁止有费用的网络调用，本轮未请求外部模型；该 skipped 不等同于扫过且干净。

## 下一轮输入

无。审查时远端实现分支仍指向冻结 H0 `819b6f8435d191ba3fad9734f5d757d28295521e`，未发现 H0 之后的新提交。

## Backlog

无其他与本轮 diff 相关的存量问题。
