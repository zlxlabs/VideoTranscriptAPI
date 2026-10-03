# PR #163 独立审查结论

Frozen range：`2995a379a898b1d1437ee02e22996e12ecebcc31..c387e2a8e15361dd807e4a0448599a5a84a60b4d`（H0）
风险级别：personal
Verdict：**PASS（仅一条非阻塞 P2 注释准确性意见；无 P1 运行时缺陷）**

failure-visibility: p2-only

## Finding

### P2-1：句末集合说明仍把相同集合描述成不同定义

- 关联 spec：第 4 条（审查原位句末对照注释与实际实现是否自洽）。
- 证据：实现现为 `dialog_segmenter.py:344` 的 `[。！？?!]`；CapsWriter 在 `capswriter_client.py:222,230` 用 `。！？!?` 并按字符成员判断，两边字符集合相同。新注释 `dialog_segmenter.py:337-338` 仍把 CapsWriter 列作“另有 3 套互不一致定义”之一；同文件旧注释 `:153-154` 还把 DialogSegmenter 写成 `。！？` 并称四套互不一致。改动后该旧对照已失实，新增对照的计数也有歧义。
- 影响：只误导维护者理解 #146 的句末集合关系，不改变切分结果、块长或时间轴。
- 定级与处置：P2，非阻塞；按本卡不修实现或测试代码，接受不修并留作维护注释清理。没有 P1，因此 P1 两问不适用。

## Spec 逐条核验

1. **通过。** 唯一运行时变化是 `DialogSegmenter._split_by_sentences` 加入 ASCII `!?`（`:344`）；`.` 未加入。该方法只由超长对话路径 `_split_long_dialog` 调用（`:73,111,125`）；尾部空白过滤和上层 `.strip()` 未变。
2. **通过。** 新测试逐项锁定中文直接切句不变（`test_dialog_segmenter_caps.py:224`），无空白尾片样本拼接回原文（`:237`），尾部空白过滤仍生效（`:243`），ASCII `.` 不切（`:249`）；连续 `?!` 的重组与既有中文 `？！` 合同一致（`:202`）。既有中文完整分块/时间线样本 `test_text_segmenter.py:472` 也通过。
3. **通过。** 两个工厂分别构造默认说话人分段器（`:23`）和模拟 `speaker_aware_processor.py:247` 的 plain 参数覆盖分段器（`:167`），均为真实 `DialogSegmenter` 实例；直接 `_split_long_dialog` 与 `segment` 路径均有覆盖（`:258,293,313`）。断言块长上限、id、时间戳首尾锚定、单调相邻连续及 `time_estimated`；完整 `segment` 结果单独计数实际 chunk，不从句子数推成本。
4. **有一条 P2。** 没有新增类型、状态、兼容或 fallback 机制。源文件原有 CRLF 保留；变更文件 `git diff --check` 通过。注释不一致见 P2-1。
5. **通过。** 仅将正则恢复成旧字符集 `[。！？]` 后，`test_direct_split_cuts_at_ascii_marks` 的 3 个参数用例均因 `AssertionError` 转红。源文件恢复 SHA-256 为 `3ce719651dc4c4dbe4cc90afcf2958e48237a2463d73c2d8129362d743266520`；355 行 CRLF、0 行裸 LF；被审实现与测试文件相对 H0 均无 diff。
6. **通过。** 本 verdict 记录逐条 spec、P2、OCR 三态、目标测试实跑和真实 CI 完整入口证据。没有 P1，不作 P1 两问结论。CI 的 `gate / primary` 为 `SKIPPED`，不当作主审通过。
7. **通过。** 本文件是唯一仓内评审产物；failure-visibility 为 `p2-only`。评审分支 commit+push 与最终 SHA/远端干净状态记录在派发报告中。

## OCR 前置

- 状态：`reviewed`；profile：`minimax`；reason：`primary_selected`；envelope 完整。
- comments：1 条，工具标注 low/documentation。意见指向 `dialog_segmenter.py:337-338` 将 CapsWriter 字符集计作不同集合，并指出 `:153-154` 及 CapsWriter 对照注释仍记旧集合。
- 独立判定：确认注释比较失实，按本仓 risk-tier 判 P2；不据工具 severity 直接定级。未发现额外运行时意见。

## 目标测试

命令：`uv run pytest -q -o 'addopts=--strict-markers' tests/unit/test_dialog_segmenter_caps.py tests/unit/test_text_segmenter.py tests/unit/test_speaker_aware_no_speaker.py tests/unit/test_speaker_aware_processor_stats.py`
结果：`96 passed, 78 warnings in 10.54s`，退出码 0。四个指定相关整文件完整运行；未在本 worktree 启动本地全量套件。

## CI 完整入口证据

- PR #163 为 draft，head `c387e2a8e15361dd807e4a0448599a5a84a60b4d`，与 H0 一致。
- Run `37120954386` / job `111196795948`（`gate / quality`）：job head 同 H0；`Tests` step 为 `success`。workflow 测试入口选中 `make test`；H0 的 Makefile 展开为 `uv sync --frozen` 后 `uv run --frozen pytest -q tests`。H0 的 pytest 配置发现 `tests/` 下测试并排除 `tests/manual/`，符合项目全量自动测试合同。
- 实际 Tests 日志进度输出计得 3,559 个测试 outcome 标记：3,554 个 `.`、5 个 `s`；没有失败标记。step success 且 runner 使用 `bash -e`，故退出码为 0。
- 同一 PR check rollup 的 `gate / primary` 为 `SKIPPED`；quality 绿灯不等于正式主审通过。派发时主干基线不可用，继承红无法判定。

## 踩到的坑

- `?`、`!` 加入捕获组后，连续 `?!` 的第二个标点会按既有配对算法成独立句片；本次样本与中文 `？！` 相同，符合冻结合同。
- 源文件为 CRLF；红验用字节替换并以原始字节校验还原，没有整文件规范化行尾。

## 闸与绕过

- 按要求先 OCR、再独立全量审 diff；CI 只使用指定真实 run/job 证据。本地只跑相关四个整文件，未并发启动全量测试。
- 变异注入被断言证实；还原后 SHA 一致、H0 文件 diff 为空。没有绕过门禁。

## 与卡面的偏差

- 无范围偏差：未读实现方报告、未修改实现/测试/配置/workflow、未审其它 PR、未部署/ready/合并。
- 本地测试命令覆盖目标文件及调用路径相关整文件；按卡面未启动 `make test`。

## 最贵的一步

- OCR 主腿等待约 6 分钟才返回完整 envelope；其后 CI job API 与日志核对花费次多，原因是需要把真实 Tests 命令、收集范围、outcome 数和 primary skip 分开确认。
