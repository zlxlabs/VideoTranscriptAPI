# 本人样本自我介绍独立复核进度

## 里程碑 1：规则改独立复核并完成窄测（2026-10-30）
- 当前阶段：implementing，窄范围测试已通过。
- 本段结论：删除了 `name_not_self_intro` 的正则句式清单；只出现在本人发言样本里的 dialog_address 改为每次 `infer()` 最多一次独立 LLM 复核。Prompt 与 schema 放在新文件，未增加 `prompts/__init__.py` 行数。
- 关键决策与已否决方案：复核请求只含 `{id, name, quote}`；缺 id 或域外分类记 `unclear` 并判 `name_not_self_intro`，调用异常沿用 `infer()` 现有路径。不在首轮推断里让模型自报 address_kind。
- 下一步唯一动作：跑 `make test`、编译检查与 scope 检查。

## 里程碑 2：全量回归与红验通过（2026-10-30）
- 当前阶段：implementing 收尾，准备交付。
- 本段结论：`make test` 退出码 0；`compileall` 退出码 0；缺 id 判 `name_not_self_intro` 与规则版本 6 两条红验均为 AssertionError。`--commit-range 5cdd6bb4..HEAD` 的 scope 检查通过。
- 关键决策与已否决方案：无。
- 下一步唯一动作：提交本段进度、推送分支并写执行器报告。

## 里程碑 3：行数闸续修，超限老文件净增压到 ≤ 0（2026-10-30）
- 当前阶段：implementing 续修第 1 轮。
- 本段结论：复核编排迁入 `apply_self_intro_review`，`speaker_inferencer.py` 只留调用点。本卡新增用例集中在 `tests/unit/test_speaker_self_intro_verifier.py`。相对 `5cdd6bb4` 四个超限文件 numstat 均为新增-删除 ≤ 0。
- 关键决策与已否决方案：不新建共享辅助文件，分发 mock 放在新测试文件里供导入。本仓没有 `scripts/lint/file_size_gate.py`，无法 `--update-baseline`；棘轮以 git numstat 相对基线为准。
- 下一步唯一动作：红验、`make test`、推送分支。
