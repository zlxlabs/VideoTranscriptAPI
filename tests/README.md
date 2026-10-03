# 测试说明

测试使用 pytest，开发依赖位于默认同步的 `dev` dependency group：

```bash
uv sync
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
| `tests/platforms/` | 仅有 `__init__.py` 和演示脚本 `demo_bilibili_metadata.py`，目前没有自动测试。 |
| `tests/manual/` | 需要人工明确确认的网络、服务或真实凭据测试。 |
| `tests/test_*.py` | 位于 `tests/` 根目录的补充测试。 |
| `scripts/perf/concurrent_load.py` | 手工并发压测脚本，不属于 pytest 回归测试。 |

## 常用命令

```bash
# 全量自动测试：发现 tests/ 下的测试，排除 tests/manual/
make test

# 卡片验证范围
uv run --frozen pytest tests/unit tests/integration

# 按需运行其他本地测试目录
uv run pytest tests/integration
uv run pytest tests/features
uv run pytest tests/llm
uv run pytest tests/transcript
uv run pytest tests/deployment
```

`make test` 是 CI 和本地的全量自动测试入口：执行 `uv sync --frozen`，再运行
`uv run --frozen pytest -q tests`。pytest 配置会排除 `tests/manual/`；根目录的
`tests/test_*.py` 和各自动测试目录都会被发现。

默认门禁会阻断指向非 loopback 地址的 AF_INET/AF_INET6 出站连接；DNS 解析、
loopback 与 Unix 域套接字放行，真实外网测试仍应放在 `tests/manual/`。

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

该开关只认 `1`：写成 `true` / `yes` / `TRUE` 等其它拼写一律不生效（手动测试
会发真实 webhook、用真实凭据，危险操作的开关应当保守）。它只控制
`tests/manual/` 下用例的收集与否，不影响其余测试，也不影响默认测试套件的
占位配置预热——预热由 `config.jsonc` 是否缺失决定。

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

## 需要 ffmpeg / ffprobe 的测试

`tests/unit/test_transcription_audio_admission.py`（#159 音轨准入）在真实共享
准入边界上验证行为：样本由 `ffmpeg -f lavfi` **现场生成**（video-only /
audio-only / mixed），准入探测跑仓库自己启动的**真实 ffprobe**，并把真实 argv、
真实 JSON、样本大小与探测耗时写成验证产物到 `data/temp/audio_admission_probe/`。
因此该文件依赖 PATH 上的 `ffmpeg` 与 `ffprobe`；缺任一者时相关用例会带明确原因
skip（`-rs` 可见），此时**不能**认为准入已被验证。其它用 mock 媒体路径的测试
（下载/缓存/临时文件等）显式注入隔离的探测替身 `_ensure_audio_track`，不复用这批
真实 CLI 断言。

## 并发压测

`scripts/perf/concurrent_load.py` 会提交本地 API 任务，并使用真实抖音和 B 站
URL。它是手工压测工具，不会被 pytest 收集，也不应在 CI 或没有明确授权的环境
运行。仅在本地服务、授权和外部访问均已确认后，才可手动运行：

```bash
uv run --extra perf python scripts/perf/concurrent_load.py
```
