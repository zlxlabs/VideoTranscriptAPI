# DESIGN-note：开放 issue 分诊（2026-11-09）—— 按根因分批根治

会话：vta-issue-triage-261109；评审：codex consult（task-id vta-issue-triage-261109）；用户已认可第一批开工、授权删除 n305 `.deploy-image`、授权 #269 真实模型验证。

## 目标

合并后：CI 不再每个 PR 白传几百 MB 缓存、不再每 job 经海外代理下载 uv；两条偶发红测试变确定；「部分完成」横幅说得出原因；发布文档与现行自动部署一致。镜像海外流量（#289/#290）先取证再定方案。

## 非目标

- 不重构缓存目录体系（#305 第 2 项 legacy 根目录回收本轮不做，见已否决）。
- 不接维护者私有 PWRS / ui-evidence-publish（#282 与仓内现行浏览器验证政策冲突，按政策关单）。
- 不给生产轮询加新的时钟抽象或重试。

## 为什么不是分区 / 删除 / 约定

- **分区**：#266 的根因就是测试把假时钟打在共享的标准库 `time` 模块上，影响整个进程；修法是把替身只绑到被测模块（分区），不是加生产时钟参数。
- **删除**：#281/#291 是删除——runner 已把 uv/npm 缓存指向宿主共享卷，setup-uv 与 setup-* 的 Actions 缓存纯属重复。#305 第 2 项删除需求本身（数据丢失风险大于多占一份文本缓存）。#295 删除过时的 `.deploy-image` 与旧手动流程描述。
- **约定**：部署事实唯一来源约定为 `.deploy-state/` + 运行容器 `.Image`，文档写清，不新增脚本。

## 方案要点与已否决方案

- **要点**：
  - 根因 R1「CI 自己装工具链 + 自己做缓存」→ #281+#291 一卡：删 `astral-sh/setup-uv`（用 runner 预装 `/usr/local/bin/uv`），`setup-node` 保留 `node-version: "22"` 但删 `cache`/`cache-dependency-path`。
  - 根因 R3「测试时钟不是确定性注入」→ #266（只替换 `mr_module.time` 的模块绑定，`requests.get` 补丁同样只绑模块）、#270（`page.clock.install({ time: start })` 统一起点，保留 pause/fastForward 所验证的行为）。两卡并行。
  - #305 第 1 项：模板展示后端已有的 `partial_count`（`utils/llm_status.py`），中英文文案。
  - 根因 R4「旧操作指引残留」→ #295：`docs/release-process.md` 对齐 push main 自动部署（`docker/DEPLOY.md` 为现行入口）；区分「最近验证成功镜像 `last_verified_good`（回滚锚点）」与「实际运行镜像（容器 `.Image`）」。n305 旧文件由主脑在用户授权下删除。
  - 根因 R2「镜像外部层浮动」→ #289 并入 #290。用户质疑「不是有缓存吗」：缓存是否跨部署保留、633MB 是 tag 漂移还是缓存不持久造成，目前未知。先派只读取证卡（读近期 deploy run 的 build 日志 CACHED 情况 + reusable workflow 的 builder 方式），据证据二选一：缓存持久 → digest 锁定即根治；缓存不持久 → ACR 基础镜像。
- **已否决**：
  - #266 加生产时钟构造参数：测试问题不该改生产接口；13574 秒是 `sum(sleeps)` 记录口径被同进程其他 sleep 污染，codex 内存探针证实可复现该口径（无落盘），不是生产轮询真等了 13574 秒。
  - #305「首次写 generation 后立即删旧根目录」：LLM 侧 `llm/core/cache_manager.py` 仍直接读根目录 `key_info.json`/`speaker_mapping.json`；两个旧变体行可共享根目录，整目录删除会绕过 `save_cache` 的引用检查 → 数据丢失。
  - #282 接 ui-evidence-publish：与 AGENTS.md 第 3 行及 `docs/testing/local-upload-browser.md` 现行政策冲突。

## 关键不变式

1. [实测] 生产轮询预算全程用 `time.monotonic()`，sleep 截断为 `min(delay, remaining)`（`downloaders/media_resolver.py:414-442`）；#266 卡的新测试锁死。
2. [实测] 预算检查在 GET 返回之后，到期后可能再发一次 GET（`media_resolver.py:420`）——严格硬上限不是当前契约；#266 卡只修测试隔离，不改此边界，报告中登记。
3. [实测] 后端 `partial_count` 已派生 PARTIAL（`utils/llm_status.py:87`），模板未展示（`web/templates/transcript.html:437`）。

## 待验证前提

1. [推断] gate-hub ci 槽 runner 镜像在 PATH 上预装 uv，并把 `UV_CACHE_DIR`/`npm_config_cache` 指向宿主共享卷——R1 卡在真实 runner 日志中验证（`which uv`、`uv cache dir`、`npm config get cache`）。
2. [推断] deploy 构建的 Docker 层缓存是否跨部署保留——R2 取证卡回答。
3. [推断] #269 英文篇幅系数 1.8 合理——在开发环境用真实凭据验证（批次 3）。

## 批次

| 批次 | 卡 | issue | 依赖 |
|---|---|---|---|
| 1 并行 | C1 CI 去重复缓存与 setup-uv | #281 #291 | 无 |
| 1 并行 | C2 测试时钟隔离 | #266 | 无 |
| 1 并行 | C3 e2e 时钟同源 | #270 | 无 |
| 1 并行 | C4 部分完成横幅原因 | #305 第 1 项 | 无 |
| 1 并行 | C5 发布文档对齐 + 本文档入库 | #295 | 无 |
| 1 并行 | R2 只读取证 | #289 #290 | 无 |
| 2 | R2 实施（digest 锁定或 ACR 基础镜像） | #289 #290 | R2 取证结论 |
| 3 | #269 真实模型 en 验证 | #269 | 无（需凭据环境） |
| 主脑 | 关 #282；删 n305 `.deploy-image` | #282 #295 | 用户已授权 |

## 验收路径

1. 入口：各卡 PR 的 CI（gate + Browser regression）全绿，且 Browser regression 日志无 `Post Run astral-sh/setup-uv`、无 setup-node 缓存上传。
2. 步骤：C2 在全量 `uv run pytest tests/unit` 顺序下通过；C4 在浏览器打开含覆盖率不足段的转录页可见段数。
3. 预期：issue 逐条按卡关闭；#305 第 2 项注明「不采纳」而非「已修」。
