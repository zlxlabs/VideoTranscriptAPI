# 测试套件加固进度

## 实施状态

- M1-M5 实施完成，待独立 Codex review 门禁。

## 里程碑提交

| 里程碑 | 提交 |
| --- | --- |
| M1 | `5b45ac4c4651122d698cdb6ea50ceffe701a5e1d` |
| M2 | `4ecbe39fcb3a56f7533ef9641d1c0ce5e24703c3` |
| M3 | `730788d0957adf5cb45ceb24a334ed5e0117eef5` |
| M4 | `7c1498f8d0a28c1f38bf380238c6aecc6663d0dc` |
| M5 | `8429b888688741ddda578236f3aa7c1bf8e1be25` |

## 独立 Codex review

- 状态：待执行；需连续 2 轮无实质新意见后解除门禁。
- 预 review 本地回归（尚不替代独立 review）：
  - `make test`：exit 0，102.88s（低于 180s）；
  - 未设置 `VTAPI_TESTS_MANUAL` 的企微手动测试：6 skipped；
  - `tests/manual -m "not network" --collect-only`：0 selected、77 deselected；
  - `tests/performance` 不存在，`scripts/perf/concurrent_load.py` AST 解析通过，
    `git diff --check` 通过。

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
- 提交：`730788d0957adf5cb45ceb24a334ed5e0117eef5`

## M4：移出 pytest 收集面的并发压测脚本

- 状态：完成。
- RED：`uv run pytest tests/performance --collect-only` 收集到
  `test_concurrent_processing` 与 `test_sequential_vs_concurrent` 两项；未执行，
  因而没有访问脚本中的真实 URL。
- 实现：将脚本迁移到 `scripts/perf/concurrent_load.py`，仅增加英文手动运行说明；
  删除已无实际测试的 `tests/performance/`（包括 M2 的目录级 conftest），并从
  `pyproject.toml` 的 `norecursedirs` 移除 `performance`。
- GREEN：
  - `uv run python -c "...ast.parse(...)..."` 输出 `syntax OK`；
  - `tests/performance` 不存在，`pyproject.toml` 无 `performance` 排除项；
  - 默认 `uv run pytest --collect-only` 不收集旧路径
    `tests/performance/test_concurrent.py`；
  - 原文件与移除新增头注释后的迁移文件 SHA-256 均为
    `500964eadba390798c89582aa58db798bfa967fadc58a4001eafc5a8c598cbed`，
    程序内容保持原样。
- 偏离：无。
- 提交：`7c1498f8d0a28c1f38bf380238c6aecc6663d0dc`

## M5：测试文档对账

- 状态：完成。
- RED：旧 README 仍引用已删除的
  `tests/performance/test_concurrent.py`、`tests/integration/test_url.py` 与
  `tests/integration/test_api.py`；也未说明 `VTAPI_TESTS_MANUAL` 强制门禁、
  `network` marker 或当前 `make test` 的 `tests/unit tests/cache` 范围。
- 实现：重写 `tests/README.md`，对齐当前测试目录、CI 基线、手动测试门禁、
  已注册 marker、常用 uv 命令及迁移后的手工压测脚本风险。
- GREEN：
  - README 列出的所有本地目录与 `scripts/perf/concurrent_load.py` 均存在；
  - README 的 CI 范围与 Makefile 的 `pytest tests/unit tests/cache -q` 一致，
    marker 与 `pyproject.toml` 一致，且无已删除路径引用；
  - `env -u VTAPI_TESTS_MANUAL uv run pytest tests/manual/test_wechat_real.py -rs`
    结果为 6 skipped；
  - `uv run pytest tests/manual -m "not network" --collect-only` 结果为
    0 selected、77 deselected。
- 偏离：无。
- 提交：`8429b888688741ddda578236f3aa7c1bf8e1be25`
