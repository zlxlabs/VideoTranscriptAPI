# C2-test-clock-isolation 进度（dlg-20261109-085514-98442b）

## 段1：替身改为模块作用域绑定 + 污染注入测试

- 当前阶段：实现完成；窄验证 `tests/unit/test_media_resolver_downloader.py` + 新文件共 87 passed；本地红验通过（base 版辅助函数放回时两条新测试全红，还原后全绿）。
- 本段结论：`make_space_downloader` 的替身安装从「共享模块属性」改为「被测模块命名空间」——`monkeypatch.setattr(mr_module, "time"/"requests", SimpleNamespace(...))`，共享 stdlib `time`/`requests` 不再被改；新增 `tests/unit/test_media_resolver_clock_isolation.py` 两条测试（他处 sleep 污染不进 sleep_log、共享模块属性恒未被替身替换），本地红验显示 base 上分别红于 `assert [0, 5.0] == [5.0]` 与 `FakeClock is builtin monotonic`。大文件净 0 行（950→950：+2 import，函数体 30→28）；曾申报 +2 超额（scope-request seq=2 获批）但压缩后未动用。
- 关键决策与已否决方案：file-size 闸（限 800，现 950）拒绝任何净增，第一次提交被拦；否决「辅助函数搬去兄弟文件」（conftest/新模块都会与 test 文件互相 import 成环）；否决「合并 import 行/删空行」（预算游戏禁区）；最终以条件表达式与单行 setattr（文件已有 131 字符行先例）把函数体压到 28 行实现净 0，未删任何测试或断言；requests 命名空间携带真 `requests.RequestException`，保住生产 `except requests.RequestException`（media_resolver.py:679/710）的捕获链不破。
- 下一步唯一动作：全量 `tests/unit` 连跑 3 次 + `-p no:randomly`，全绿后补段2。

## 段2：全量验证通过

- 当前阶段：验证完成，待收尾（报告、scope 自检、push、PR）。
- 本段结论：最终提交态（e385ab9d）上 `uv run pytest tests/unit -W ignore` 连跑 3 次全绿，汇总行均为 `4181 passed, 3 skipped, 20 subtests passed`（246.03s / 272.27s / 307.93s）；`-p no:randomly` 一次同绿（224.55s）。pytest-randomly 未安装，随机序附加一轮不适用，以 no:randomly 轮代替。
- 关键决策与已否决方案：否决「沿用 952 行版本的旧三轮结果」——压缩改写后必须在最终提交态重取证据。
- 下一步唯一动作：写 report.md、过 report contract check 与 card_scope_check，push 建 PR（正文 Refs #266）。
