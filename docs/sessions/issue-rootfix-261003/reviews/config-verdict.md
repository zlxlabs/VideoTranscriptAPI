# PR #162 配置诊断独立验收

failure-visibility: p2-only

## Verdict

**findings**（risk-tier: personal）。审查对象固定为 `2995a379a898b1d1437ee02e22996e12ecebcc31..a8b0251d13fe956303a5894840c59c0716cd2aac`。

## Findings

### P2：ASCII stdout 遇到合法非 ASCII 策略值时检查命令失败

- **违反 spec #5**：`main.py:89-94` 用 `json.dumps(..., ensure_ascii=False)` 输出白名单；`LLMConfig.from_dict` 在 `src/video_transcript_api/llm/core/config.py:190-195` 原样接受非空 `fallback_strategy`，校验未限制字符集。`CLAUDE.md:13` 要求 console 日志使用纯英文。
- **实测**：真实运行 `main.py --check-config` 子进程，输入合法策略值 `策略-测试`，设置子进程 `PYTHONIOENCODING=ascii`，退出码为 1；stdout 已有 `Configuration OK`，随后抛 `UnicodeEncodeError`。临时配置位于系统临时目录，未生成运行期路径。当前评审 shell 的 `locale charmap` 和 Python stdout 编码均为 UTF-8，所以该环境下不触发；这是个人使用下的 CLI 编码边缘故障，判 P2。
- **处置建议**：默认 `ensure_ascii=True` 会保持 ASCII 输出，JSON 解码值不变。本卡不改实现。

### P2：示例把 plain 结构化路由描述为阈值分段之后的选择

- **违反 spec #4**：新增 `config/config.example.jsonc:276-277` 写成“分段之后”再由 `structured_calibration_for_plain` 选择结构化或旧纯文本路由，并未说明结构化路由依赖原始 segments。
- **代码证据**：`src/video_transcript_api/api/services/llm_ops.py:1149-1153` 在交给协调器前先尝试把 plain 输入变为 segments；无 segments 时返回 transcript。`src/video_transcript_api/llm/coordinator.py:436-452` 按输入类型分别路由：字符串进入 `PlainTextProcessor`，列表进入 `SpeakerAwareProcessor`。阈值判断只在 `src/video_transcript_api/llm/processors/plain_text_processor.py:121-138` 的字符串路径中发生。因此阈值分段在 plain 结构化路由之后；即使开关为 true，无可用原始 segments 时也会回到纯文本路径（`llm_ops.py:1087-1091,1117-1120`）。注释顺序及无条件承诺会误导配置维护者；仅文档注释有误，未改变运行行为，判 P2。

## Spec checklist

1. **部分符合**：`main.py:85-94` 保留原校验和 `Configuration OK`，随后输出稳定 JSON；`llm_effective` 内恰有 9 个白名单键，每键仅 `value`、`source`，没有凭据字段。非 ASCII 序列化见 P2-1。
2. **符合**：值来自同输入的 `LLMConfig.from_dict`（`main.py:31,48-62`）；8 个 `.get` 字段按键存在判断来源，fallback 按真实 truthiness 派生。测试覆盖显式 `null`、`False`、缺失及 fallback 假值（`tests/unit/test_check_config_effective_values.py:156-244`）。
3. **符合**：新增单测通过真实 `subprocess.run` 执行 CLI，传入绝对 argv 和最小 env，解析真实 stdout JSON，检查凭据 sentinel 与运行期路径；缺少 llm 段仍走原错误路径（同测试 `:85-123,254-317`）。主分支仍先完成 `load_and_validate_config` 再打印成功行。未读运行配置或生产凭据。
4. **部分符合**：阈值注释明确为严格大于，与 `plain_text_processor.py:121` 一致；新增路由注释顺序错误，见 P2-2。
5. **部分符合**：无凭据字段进入白名单；合法非 ASCII 策略值与 ASCII stdout 冲突，见 P2-1。当前 shell 实测编码为 UTF-8。
6. **符合**：把 `main.py:51` 的来源映射临时变异为恒 `default`，先确认注入行，再运行 `test_explicit_values_report_config_source`；该测试以 `AssertionError` 在 `tests/unit/test_check_config_effective_values.py:191` 转红（exit 1）。只恢复该行；恢复后相对 H0 的实现 diff 为空，工作区干净。
7. **符合**：本 verdict 提供文件/行号、两条 P2、全量测试、OCR 三态、逐条 spec 结论及剩余风险。
8. **符合**：本评审仅新增此 verdict；本文件随评审分支提交并推送，SHA 与干净状态写入派发报告。

## 全量测试

- 命令：`make test`（Makefile 正式入口：`uv sync --frozen` + `uv run --frozen pytest -q tests`）。在本 worktree 独立 `.venv` 中运行一次。
- 结果：exit 0，`real 154.03s`。完整输出已归档于派发目录 `make-test.log`。
- pytest 输出未给最终汇总行；按本次完整进度输出计 3,524 个通过标记、3 个 skip 标记，共 3,527 项；无失败标记。运行有弃用告警。pytest 缓存的 nodeid 数与本次进度标记不一致，未用缓存计数冒充测试数。

## OCR 前置

- `ocr-review` exit 0；JSON envelope 完整，`status=reviewed`、`reason=primary_selected`、`cli_status=complete`、`coverage=complete`；stderr 记主腿耗时 `575.833s`。
- 1 条意见：`main.py:89-93` 的 `ensure_ascii=False` 可能让合法非 ASCII fallback 策略在 ASCII stdout 上触发 `UnicodeEncodeError`。工具标注 `medium` / `confirmed`；独立子进程探针复现后，本仓按 personal 风险判 **P2**，不是 P1。
- OCR 结果完整归档于派发目录 `ocr-stdout.json` 与 `ocr-stderr.txt`；OCR 未替代全量独立审查。

## 边界与遗留

- 未修改实现、测试或配置；来源标签红验后 `main.py` 相对 H0 完全无差异。
- 卡面 Diff-Lines-Hard 为 240，但冻结 PR diff 实测为 504 insertions、2 deletions（6 文件）；`budget_diff/base_sha/head_sha` 未提供。作为 reviewer 按要求审完固定范围，没有扩写 PR。
- 未核验 PR Ready 或最终 CI；按卡面留给主脑取证。派发时主干基线不可用，继承红未能判定；本地全量测试本次为绿。
