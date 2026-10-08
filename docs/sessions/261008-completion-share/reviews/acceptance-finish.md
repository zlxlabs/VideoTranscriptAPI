# 完成分享回执验收收尾（2026-10-08）

## 范围

仅补 `summary_status="disabled"` 的持久测试锁，并在设计测试表登记该 node；没有改生产实现、依赖、配置、schema、网页、阈值或守卫。新增测试输入真实形态的 `terminal_snapshot.result.stats.summary_status="disabled"`，同时故意携带不可发布的总结正文，以验证该状态不会复制总结内容。

测试 node：`tests/unit/test_completion_share.py::test_disabled_summary_receipt_keeps_urls_without_summary_paragraph`。

该 node 分别断言“总结未启用”、原始地址 `https://example.test/video?id=disabled&track=keep`、绝对 view URL `https://view.test/view/disabled-token`，并断言携带的总结正文不出现在回执；最后还锁定完整回执文本。

## 验证

### 窄测

命令：

```bash
UV_PROJECT_ENVIRONMENT="$PWD/.venv" uv run pytest -o addopts='' -q tests/unit/test_completion_share.py tests/unit/test_notification_e2e_delivery.py tests/unit/test_cache_task_recovery.py
```

结果：退出码 0；`55 passed, 154 warnings in 10.11s`。使用本工作树 `.venv`。

### 既有缓存关闭时限测试对照

对照 node：`tests/unit/test_cache_task_recovery.py::TestDrainNonTerminalTasksOnShutdown::test_deadline_budget_stops_early_and_leaves_remainder_non_terminal`。

冻结基线 `3a0c8a1be4c6714bd8d528d86ba6114864fd526a`：通过 `scratch-worktree.sh` 建一次性树，并在该树内以 `UV_PROJECT_ENVIRONMENT="$PWD/.venv"` 运行同一 node；结果 `1 passed, 154 warnings in 0.33s`，退出码 0。基线树独立创建自己的 `.venv`。

现分支：`UV_PROJECT_ENVIRONMENT="$PWD/.venv" uv run pytest -o addopts='' -q tests/unit/test_cache_task_recovery.py::TestDrainNonTerminalTasksOnShutdown::test_deadline_budget_stops_early_and_leaves_remainder_non_terminal`；结果 `1 passed, 154 warnings in 0.33s`，退出码 0。

此前独立审查记录的全量运行曾因该 node 用时 `0.428s > 0.3s` 失败。当前基线与现分支各只跑一次且本次均通过；这不足以证明无随机超时，也不能归因于基线或现分支，归因仍未定，不将此前失败标成假红。

### 全量

命令：

```bash
UV_PROJECT_ENVIRONMENT="$PWD/.venv" make test
```

结果：退出码 0，`tests` 进度到 `[100%]`；仓库默认双 `-q` 不打印最终通过计数。`make test` 在本工作树临时创建 `config/config.jsonc` 并于退出清理；没有使用主仓的虚拟环境。输出含既有弃用警告。

### disabled 测试有效红验

一次性 H0 scratch 树基于真测试提交 `3b6c2a74737cb6555a2fbc293d786d8c3361120d` 创建，使用 scratch 树自己的 `.venv`。仅把 `src/video_transcript_api/utils/notifications/completion_share.py` 的 disabled 分支提示暂时从：

```python
elif summary_status == "disabled":
    notice = "ℹ️ 总结未启用"
```

改为：

```python
elif summary_status == "disabled":
    notice = "ℹ️ 总结配置错误"
```

运行命令：

```bash
UV_PROJECT_ENVIRONMENT="$PWD/.venv" uv run pytest -o addopts='' -q tests/unit/test_completion_share.py::test_disabled_summary_receipt_keeps_urls_without_summary_paragraph
```

测试以退出码 1 失败，原始错误为：

```text
>       assert "总结未启用" in receipt
E       AssertionError: assert '总结未启用' in '✅ [#disabl] 禁用总结标题\n原始地址：https://example.test/video?id=disabled&track=keep\n\n总结和校对：https://view.test/view/disabled-token\n\nℹ️ 总结配置错误'
```

随后将 `总结配置错误` 精确还原为 `总结未启用`，并运行 `git diff --exit-code -- src/video_transcript_api/utils/notifications/completion_share.py`，退出码 0、无 diff。scratch-worktree 收尾删除一次性树。该红验是 `AssertionError`，不是导入错误。

## 冻结实现与文件范围

冻结实现未改。以 `b889bd3d6d8c4b6f663b626de6907dce25a58ffc` 为 Base 检查 `git diff --name-only Base..HEAD` 时，允许文件应且仅应为：

- `tests/unit/test_completion_share.py`
- `docs/sessions/261008-completion-share/design.md`
- `docs/sessions/261008-completion-share/reviews/acceptance-finish.md`

本次不进行生产通知、部署、合并或 PR ready；PR #197 保持 Draft。队列提交成功不等同真实平台已收信。
