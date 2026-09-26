# 测试说明

测试使用 pytest，建议先同步开发依赖：

```bash
uv sync --extra dev
```

## 目录结构

| 路径 | 用途 |
| --- | --- |
| `tests/unit/` | 快速单元测试。 |
| `tests/cache/` | 缓存行为测试。 |
| `tests/integration/` | 组件集成测试。 |
| `tests/features/` | 功能级测试。 |
| `tests/llm/` | LLM 相关的本地测试。 |
| `tests/transcript/` | 转录兼容性与转换测试。 |
| `tests/deployment/` | 部署及健康检查相关测试。 |
| `tests/platforms/` | 平台适配器测试。 |
| `tests/manual/` | 需要人工明确确认的网络、服务或真实凭据测试。 |
| `tests/test_*.py` | 位于 `tests/` 根目录的补充测试。 |
| `scripts/perf/concurrent_load.py` | 手工并发压测脚本，不属于 pytest 回归测试。 |

## 常用命令

```bash
# 本地快速基线：unit 和 cache
make test

# 卡片验证范围
uv run --frozen --extra dev pytest tests/unit tests/integration

# GitHub Required Gate v2 的 legacy quality 入口：uv sync --frozen 后执行全套 pytest
uv sync --frozen
uv run --frozen pytest -q

# 按需运行其他本地测试目录
uv run pytest tests/integration
uv run pytest tests/features
uv run pytest tests/llm
uv run pytest tests/transcript
uv run pytest tests/deployment
```

`make test` 当前只覆盖 `tests/unit tests/cache`。GitHub workflow 调用
`zlxlabs/gate/.github/workflows/gate-v2.yml@v2`；当仓库没有可执行的
`scripts/gate-quality` 时，quality job 使用 legacy 步骤，运行 `uv sync --frozen`
和 `uv run --frozen pytest -q`。本地测试前同步 `dev` extra，避免 `pytest` 回落到
系统解释器。

任务观测回归：`tests/unit/test_task_observability_report.py` 覆盖只读 CLI、旧 schema、
UTC 窗口、去重、缺字段与失败判据；`tests/integration/test_task_observability.py`
贯通 notes worker、cache/audit SQLite 与 CLI 子进程，验证归档修复、缓存清理和迁移。

## 手动测试门禁

`tests/manual/` 默认自动发现时被排除；即使显式传入某个手动测试文件，未设置
环境变量也会被跳过：

```bash
# 安全：收集并显示手动测试在默认情况下会被 skip
uv run pytest tests/manual/test_wechat_real.py -rs

# 仅验证已明确选择手动模式后的收集结果，不执行测试体
VTAPI_TESTS_MANUAL=1 uv run pytest tests/manual/test_wechat_real.py --collect-only
```

只有在明确了解真实网络、webhook 和凭据影响时，才设置
`VTAPI_TESTS_MANUAL=1` 执行手动测试。请勿将会发送 webhook 的测试作为常规
验收命令运行。

## pytest markers

项目已注册以下 marker：

- `unit`：快速、无 I/O 的单元测试。
- `integration`：可能依赖服务的集成测试。
- `slow`：耗时较长或使用大数据的测试。
- `network`：访问真实外部服务的测试。

显式收集 `tests/manual/` 时，目录级配置会自动为所有项添加 `slow` 和
`network`。例如，以下命令可验证 marker 兜底不会选择手动网络测试：

```bash
uv run pytest tests/manual -m "not network" --collect-only
```

## 并发压测

`scripts/perf/concurrent_load.py` 会提交本地 API 任务，并使用真实抖音和 B 站
URL。它是手工压测工具，不会被 pytest 收集，也不应在 CI 或没有明确授权的环境
运行。仅在本地服务、授权和外部访问均已确认后，才可手动运行：

```bash
uv run --extra perf python scripts/perf/concurrent_load.py
```
