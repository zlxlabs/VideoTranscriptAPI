# 测试套件加固 + CI 覆盖补齐 —— Codex 交接

> Session ID：`260723-0605-test-hardening`
> 创建：2026-07-23（EDT）
> **状态：未启动（方案已定稿，交接 Codex 实施）**
> 类型：测试基建加固，小批量、低风险，不动 src/ 业务代码
> 基线分支：`main`
> 建议工作分支：`test/suite-hardening`（worktree 内）

---

## 背景事实（2026-07-23 盘点结论，不要重新调研）

原任务设想是「CI 慢：mock 掉真实外网测试」，盘点后发现**该问题已于 2026-07-15 由 commit `9371d52` 解决**：真实外网用例全部移入 `tests/manual/`（`pyproject.toml` 的 `norecursedirs` 排除），CI Tests 步骤从 15-25 分钟降到约 16 秒。

当前 CI 链路：`.github/workflows/gate.yml` → `zlxlabs/gate@main` 复用 workflow → 检测到 Makefile 有 `test:` target → 执行 `make test` = `uv run --frozen --extra dev pytest tests/unit -q`。**CI 只跑 `tests/unit`**（120 文件 ~2235 用例，已逐一核实全部 mock 干净、无真实外联）。

盘点发现 4 个残留风险 + 1 个覆盖缺口，即本次任务：

1. **`tests/manual/` 无代码级门控**：目录排除只防「默认发现」，谁显式 `pytest tests/manual/test_wechat_real.py` 就会**真发企微消息**（硬编码 webhook：`test_wechat_real.py:29`、`test_feishu_real.py:17`）。docstring 建议的 `VTAPI_TESTS_MANUAL=1` 没有对应的 skipif 强制。
2. **marker 零使用**：`pyproject.toml:48-60` 声明了 `unit`/`integration`/`slow` 三个 marker，全仓库零命中。分类完全靠目录物理约定，没有 `-m` 兜底。
3. **`tests/performance/test_concurrent.py` 是隐雷**：`async def` 测试但没挂 `pytest.mark.asyncio`，目前靠「没人跑」不爆雷；pytest-asyncio 配置一变就可能被意外执行（内含真实抖音/B站 URL 的并发压测）。
4. **`tests/README.md` 漂移**：第 14-38 行还写着 `integration/test_url.py`、`integration/test_api.py`、`unit/test_generic_url.py`，实际早已在 `tests/manual/`。
5. **CI 覆盖缺口**：本仓交接纪律一直要求本地跑 `tests/unit tests/cache` 全绿，但 CI 只跑 `tests/unit`。`tests/cache`（14 文件 ~59 用例）已核实无外联，本地 unit+cache 全套 62 秒。其余目录（integration ~89 / features ~58 / llm ~30 / transcript ~3 / deployment ~20 / tests 根目录散文件 ~18）也初步核实无真实外联，但未验证是否全绿、是否稳定。

---

## 任务清单（M1-M5，每个独立 commit）

### M1. `tests/manual/` 强制 skip 门控（P1，防误伤）
- 新建 `tests/manual/conftest.py`：`pytest_collection_modifyitems` 里检查 `os.environ.get("VTAPI_TESTS_MANUAL") != "1"` 时给该目录全部 item 加 `pytest.mark.skip(reason="manual tests require VTAPI_TESTS_MANUAL=1")`。
- 与 `tests/conftest.py:47-107` 已有的 `VTAPI_TESTS_MANUAL` 语义保持一致（同一个环境变量，同样解释）。
- 验收：`uv run pytest tests/manual/test_wechat_real.py --collect-only` 正常收集；不设环境变量直接跑显示全部 skipped、**零消息发出**；验证「设了变量能跑」用 `--collect-only` 或挑无副作用用例，**禁止在验收中真发 webhook**。

### M2. marker 防护网（P2）
- 在 `pyproject.toml` markers 里新增 `network: tests that hit real external services`（已有 `--strict-markers`，不注册会报错）。
- 在 M1 的 `tests/manual/conftest.py` 与新建 `tests/performance/conftest.py` 里通过 `pytest_collection_modifyitems` 自动给全部 item 打 `slow` + `network` 标记，不逐文件手改。
- 验收：`uv run pytest tests/manual -m "not network" --collect-only -q` 显示 0 selected（配合 deselected 计数）。

### M3. CI 覆盖补齐（P1，本批核心收益）
- `Makefile` 的 `test:` target 从 `pytest tests/unit` 改为 `pytest tests/unit tests/cache`（这步是确定的，直接做）。
- 对候选目录逐个本地计时评估：`tests/integration`、`tests/llm`、`tests/features`、`tests/transcript`、`tests/deployment`、`tests/` 根目录 4 个散文件。纳入规则（自主执行，无需请示）：
  - 该目录单独跑**全绿**且耗时合理（纳入后 `make test` 本地总时长 ≤ 3 分钟）→ 纳入 Makefile；
  - 有失败/flaky/依赖外部服务的 → 不纳入，在 PROGRESS.md 记录目录、失败用例名和原因；
  - **禁止**为了纳入而修改 src/ 业务代码或删测试；测试失败若疑似真 bug，记录不修，留给后续任务。
- 验收：`make test` 本地 exit 0；PROGRESS.md 有每个候选目录的计时与纳入/排除决定表。

### M4. `tests/performance/test_concurrent.py` 移出 pytest 收集面（P3）
- 移到 `scripts/perf/concurrent_load.py`（保留内容原样），文件头加注释说明用途和手动运行方式；`tests/performance/` 目录若因此为空则删除，并同步清理 `pyproject.toml` `norecursedirs` 里的 `"performance"` 项。
- 不要把它修成有效的 pytest 用例——它是压测脚本，不是回归测试。

### M5. `tests/README.md` 对账（P3）
- 更新为当前实际目录结构与运行方式（`make test` 范围、`VTAPI_TESTS_MANUAL` 门控、marker 说明）。

---

## 工程纪律

1. 在独立 worktree + 分支 `test/suite-hardening` 工作，基于最新 `origin/main`；不直接在主工作区改。
2. `uv sync --extra dev` 后再跑测试；pytest ini 已带 `-q` 不要再加，以 exit code 为准。
3. console/日志输出纯英文；与用户沟通用中文。
4. 每个 M 独立 commit，message 中文祈使句（如「给 tests/manual 加 VTAPI_TESTS_MANUAL 强制门控」）。
5. 不动 `src/`、不动 `.github/workflows/`、不动 zlxlabs/gate 仓库。
6. 完成后本地跑 codex review（read-only）复现 CI gate，连续 2 轮无实质新意见。
7. 进度写本目录 `PROGRESS.md`（commit 列表、M3 计时决策表、测试结果）。

## 完成判据

- [ ] 不设 `VTAPI_TESTS_MANUAL` 时显式跑 `tests/manual/` 任意文件全部 skipped，零外发消息
- [ ] `network`/`slow` marker 注册并自动打标，`-m "not network"` 可兜底排除
- [ ] `make test` 至少覆盖 `tests/unit tests/cache`，本地 exit 0
- [ ] M3 候选目录逐个有「纳入/排除 + 原因 + 耗时」记录
- [ ] `tests/performance/` 压测脚本移出 pytest 收集面
- [ ] `tests/README.md` 与实际结构一致
- [ ] PROGRESS.md 记录全部 commit hash

## 关键文件索引

| 用途 | 路径 |
|---|---|
| pytest 配置（markers/norecursedirs） | `pyproject.toml:48-60` |
| CI 测试入口 | `Makefile:1-4`（`test:` target） |
| 全局 conftest（VTAPI_TESTS_MANUAL 已有语义） | `tests/conftest.py:47-107` |
| 真发企微/飞书的高危文件 | `tests/manual/test_wechat_real.py:29`、`tests/manual/test_feishu_real.py:17` |
| 失效 async 压测 | `tests/performance/test_concurrent.py` |
| 文档漂移 | `tests/README.md:14-38` |
| 上次外网测试治理（背景参考） | commit `9371d52`（2026-07-15） |
