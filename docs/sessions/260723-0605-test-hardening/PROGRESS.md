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
- 提交：待本提交生成（后续里程碑补充哈希）。
