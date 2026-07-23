# 测试套件加固进度

## M1：`tests/manual/` 强制 skip 门控

- 状态：完成。
- 实现：新增 `tests/manual/conftest.py`。当 `VTAPI_TESTS_MANUAL != "1"` 时，
  在收集阶段为该目录全部测试项添加
  `skip(reason="manual tests require VTAPI_TESTS_MANUAL=1")`。
- RED：改动前执行
  `uv run pytest tests/manual/test_wechat_real.py --collect-only -m skip`，结果为
  `no tests collected (6 deselected)`；同文件普通收集为 6 项，证明当时没有 skip
  门禁。
- GREEN：
  - `env -u VTAPI_TESTS_MANUAL uv run pytest tests/manual/test_wechat_real.py --collect-only`
    收集 6 项；
  - `env -u VTAPI_TESTS_MANUAL uv run pytest tests/manual/test_wechat_real.py`
    结果 `6 skipped`，未执行 webhook 测试体；
  - `VTAPI_TESTS_MANUAL=1 uv run pytest tests/manual/test_wechat_real.py --collect-only`
    收集 6 项且未执行测试。
- 已知基线问题：`env -u VTAPI_TESTS_MANUAL uv run pytest tests/manual --collect-only`
  在 `tests/manual/test_loguru_migration.py` 收集阶段因
  `ImportError: cannot import name 'logger' from 'video_transcript_api.utils'` 失败；
  此问题不属于 M1，未处理。
- 提交：`5b45ac4c4651122d698cdb6ea50ceffe701a5e1d`

## M2：marker 防护网

- 状态：完成。
- 实现：注册 `network: tests that hit real external services`；在
  `tests/manual/` 和 `tests/performance/` 的目录级收集钩子中为全部测试项自动
  添加 `slow` 与 `network` marker。
- RED：
  - `uv run pytest --markers | rg '^@pytest\\.mark\\.network'` 无输出，证明
    `network` 尚未注册；
  - `env -u VTAPI_TESTS_MANUAL uv run pytest tests/manual/test_wechat_real.py -m network --collect-only`
    结果为 `no tests collected (6 deselected)`，证明未打标；
  - 原样验收命令在改动前会收集非 network 项，并因
    `test_loguru_migration.py` 的失效 `logger` 导入中断。
- GREEN：
  - `uv run pytest --markers | rg '^@pytest\\.mark\\.network'` 显示注册说明；
  - `uv run pytest tests/manual -m "not network" --collect-only -q` exit 0，
    未输出已选项目（0 selected）；
  - `uv run pytest tests/manual -m network --collect-only -q` 收集全部 76 项；
  - `uv run pytest tests/performance -m network --collect-only -q` 收集 2 项；
  - `env -u VTAPI_TESTS_MANUAL uv run pytest tests/manual/test_wechat_real.py -rs`
    仍为 6 skipped。
- 偏离：为使原样的全目录收集验收可执行，移除了
  `test_loguru_migration.py` 中已不再由 `video_transcript_api.utils` 导出的
  全局 `logger` 导入及唯一调用。该调用只验证已移除 API，保留的
  `setup_logger` 调用继续覆盖此手工冒烟测试的现行 API。
- 提交：`4ecbe39fcb3a56f7533ef9641d1c0ce5e24703c3`

## M3：CI 覆盖补齐

- 状态：完成。
- RED：`make -n test` 显示测试目标仅执行 `pytest tests/unit -q`，不含
  `tests/cache`，存在已确认的 CI 覆盖缺口。
- 实现：Makefile `test` 目标纳入确定范围 `tests/unit tests/cache`。

| 范围 | 命令结果 | 耗时 | 决策与原因 |
| --- | --- | --- | --- |
| `tests/integration` | 92 passed | 15.55s | 排除：单独全绿，但与其他候选聚合运行至 2:56 仍未完成，无法证明总时限 ≤ 180s。 |
| `tests/llm` | 30 passed | 1.99s | 排除：同上，保守维持已确定的 CI 范围。 |
| `tests/features` | 52 passed | 15.37s | 排除：同上；日志中的 webhook 为占位 URL。 |
| `tests/transcript` | 3 passed | 1.38s | 排除：同上。 |
| `tests/deployment` | 20 passed | 5.11s | 排除：同上。 |
| 根目录散文件：`test_enhanced_logging.py`、`test_markdown_list_rendering.py`、`test_metadata_override.py`、`test_view_token_fix.py` | 18 passed | 1.67s | 排除：同上；已显式列举，未递归重复收集。 |
| `tests/cache` | 由最终 `make test` 覆盖 | 见下方 | 确定纳入范围。 |

- GREEN：
  - `uv sync --extra dev` 完成；
  - `make test`：exit 0，65.30s；执行范围 `tests/unit tests/cache`，
    `--collect-only` 确认共 2586 项，满足 180s 时限。
- 提交：待本提交生成（后续里程碑补充哈希）。
