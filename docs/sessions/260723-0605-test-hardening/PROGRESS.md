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
- 提交：待本提交生成（后续里程碑补充哈希）。
