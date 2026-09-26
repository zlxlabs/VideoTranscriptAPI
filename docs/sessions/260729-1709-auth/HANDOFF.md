# 统一浏览器 Bearer 鉴权：新 Session 交接

## 当前现场

- 仓库：`~/projects/personal/VideoTranscriptAPI`
- main HEAD：`6e2bac9583d68516d1723d7606e0581e2f01a32f`，与 `origin/main` 同步。
- PR #37（PWA）已经合并，旧分支和旧 worktree 已清理。
- 主工作区有用户改动，必须原样保留：`M TODOS.md`、未跟踪的 `docs/sessions/260715-0635-pr3x-gate/HANDOFF.md`、`docs/sessions/260729-1150-pwa-handoff/README.md`。
- 目前只有 `.github/workflows/gate.yml`，没有接入 D3；本任务不部署。
- 风险等级：`internal`（3–5 人使用）。

下面的代码块是可直接复制到新 Codex session 的完整执行 Prompt。

## 可复制执行 Prompt

```text
你正在 `~/projects/personal/VideoTranscriptAPI` 实现“统一三页面浏览器 Bearer 鉴权”增量。请先读仓库根目录 AGENTS.md（中文沟通，console 优先英文），并严格遵守其中的委派、TDD、worktree、评审和提交约定。

【第一优先级：隔离工作区】
在任何应用代码、测试、依赖、配置或生成文件写入前，从最新 `origin/main` 创建同文件系统的新 worktree：

  repo=~/projects/personal/VideoTranscriptAPI
  wt=~/projects/personal/VideoTranscriptAPI-auth-refactor
  git -C "$repo" fetch origin main
  git -C "$repo" worktree add -b feat/unified-browser-auth "$wt" origin/main

建议路径和分支分别是 `~/projects/personal/VideoTranscriptAPI-auth-refactor` 与 `feat/unified-browser-auth`。如果路径或分支已存在，只做只读核查，禁止删除、覆盖、强制 reset 或 checkout 覆盖；有冲突就停下并报告。所有代码/测试/依赖/提交/push/PR 只在该 worktree 完成。原主工作区只读，绝不复制、提交或清理其中的脏文件（特别是 `TODOS.md` 和已有 `docs/sessions`）。所有实质写入必须显式委派给 `implementer`；仓库级或批量只读探索委派给 `explorer`，主代理只负责规划、审查和验收。

【目标与边界】
统一三页面的浏览器 Bearer 鉴权，使缓存命中不再弹 API Key。后端 `verify_token`、权限/所有权检查、公有 `view_token` 阅读语义保持不变；这是 3–5 人的 internal 服务，不引入 cookie 或 OIDC。

明确 NOT scope：后端鉴权/权限改造、cookie/OIDC、全项目 CSP/CORS、rate limit、TTL/rotation、全量本地历史删除、部署。

【必须保持的不变式】
1. 新增 `src/web/static/js/auth-storage.js`；canonical 键是完整字面量 `vta_bearer_token`。继续使用现有 Base64 + 反转 + 固定后缀格式；解码必须验证固定后缀，损坏值按 absent 处理。
2. 凭据读取优先级固定为：canonical > `localStorage` 的 `api_key` > `localStorage` 的 `vta_api_key_persist` > `sessionStorage` 的 `vta_api_key`。成功写 canonical 后删除别名。
3. 用户主动选择 A 后写 `vta_auth_migration_v1` 标记；迁移成功或主动清除后不再读旧键。监听 `storage` 事件，让其他标签页清除 session 别名，旧凭据不可复活。
4. `localStorage` 出现 `SecurityError` 或 `QuotaExceededError` 时降级到内存，并只提示一次；拒绝控制字符。提供可检索的 `buildAuthHeaders`；401 只在 token 快照仍相同的情况下 compare-and-clear。
5. `app.js` 只适配 token 路径，其他 `StorageManager` 偏好和语义不动。
6. history 删除“记住双轨”：change/clear 必须 abort 私有请求，并原子清空 list/stats/filter/selection/tooltip/current identity；不能删除主题或无关任务历史。
7. 新增 `transcript-protected-action.js` 统一 recalibrate/resummarize/generate notes：缓存命中可直接提交；缺失或首次 401 才弹单一对话框；最多重放一次；403/404/409 不换 token、不重试；POST `TypeError`/网络错误不得自动重放。
8. 轮询间隔 3 秒、最长 600 秒、连续错误阈值 10；单 in-flight、单 timer。只接受状态白名单；任务可能 HTTP 200 且 body `code=500`，以 `data.status` 为准；unknown、非 JSON、缺 `task_id` 都必须显式失败；`pagehide` 必须 abort。
9. PWA 已合并：鉴权脚本必须纳入 `sw.js` 缓存升级/版本失效策略，并有模板加载顺序测试；复用已有 `escapeHTML`。共享脚本加载失败时，必须显式禁用受保护操作。
10. 提供 clear/change 入口；dialog 具备 focus、Escape、Enter 和 ARIA 行为；日志不能记录 token 或完整敏感 URL。

【工程与命名】
- TDD：先写失败测试，再实现；每个可独立绿的增量 ≤3500 行，绿后 commit + push。先开 draft PR。
- 遵守可检索命名：导出符号 2–4 词且至少一个领域词；文件名带领域；每个导出上方写说明单位/归属/时序等尖锐约束；错误和事件使用完整可 grep 的字面量。
- 优先成熟包；如能显著提高真实浏览器契约测试质量，可新增 `jsdom` devDependency，但不要为此扩大范围。
- 原有后端鉴权、权限/所有权、公开 `view_token` 阅读路径只做兼容测试，不改行为。

【测试与验收】
至少运行并保留证据：

  npm run test:web

新增前端契约 pytest；运行相关后端 auth/history/task suites，并在时间允许时运行合理的全 unit。验收必须覆盖：缓存命中无弹窗；legacy 只迁移一次且 clear 后不可复活；并发 401 单弹窗、单次重放；POST 网络错误无重复提交；轮询无重叠且终态正确；clear/change 后无私有旧数据残留；PWA 升级后加载新脚本。

实现完成后先跑 OCR 前置扫描（工具可用才跑；不可用须记录原因），再做只审本次 diff 的 internal review 循环；按 internal 规则连续 2 轮无新增 P1 才收敛。最后做 live browser/design QA。评审输入需附本 Prompt/规格与风险等级；P1 必修，P2/P3 可接受不修但要记录理由。不要部署。

可参考的评审产物（只读，不要改写）：
  ~/.gstack/projects/zj1123581321-VideoTranscriptAPI/zlx-main-eng-review-test-plan-20260729-161411.md
  ~/.gstack/projects/zj1123581321-VideoTranscriptAPI/tasks-ceo-review-20260729-154836.jsonl
  ~/.gstack/projects/zj1123581321-VideoTranscriptAPI/tasks-eng-review-20260729-161507.jsonl

【交付纪律】
- 仅在新 worktree 提交、push 和开/更新 draft PR；每个增量报告 commit、测试、review 和剩余风险。
- 不删除、强制 reset、覆盖 checkout 任何现有工作区或分支；若 worktree 清理需按 AGENTS.md 的合并后规则执行。
- 最终验收逐条回答“代码在哪、哪个测试锁死”；未能回答即视为未完成。
```

## 新 Session 第一阶段命令与检查清单

先在原主工作区只读执行以下命令；任何一项不符合预期都停止，不要删除或覆盖：

```bash
repo=~/projects/personal/VideoTranscriptAPI
wt=~/projects/personal/VideoTranscriptAPI-auth-refactor
git -C "$repo" status --short
git -C "$repo" fetch origin main
git -C "$repo" rev-parse origin/main
git -C "$repo" worktree list
git -C "$repo" branch --list feat/unified-browser-auth
test ! -e "$wt" || { echo "worktree path exists; inspect read-only and stop"; exit 1; }
git -C "$repo" worktree add -b feat/unified-browser-auth "$wt" origin/main
git -C "$wt" status --short
```

- [ ] 已确认 `origin/main` 是最新基线，且新 worktree 与主仓在同一文件系统。
- [ ] 已确认目标路径和 `feat/unified-browser-auth` 不存在；若存在，只读核查并报告冲突。
- [ ] 已确认主工作区的 `M TODOS.md` 和两个未跟踪 handoff/README 仍在，未被复制、删除或提交。
- [ ] 已在新 worktree 读取 AGENTS.md，并完成 blindspot/计划；实质写入已委派 `implementer`。
- [ ] 未运行部署流程；未修改后端鉴权权限、cookie/OIDC、CSP/CORS、rate limit、TTL/rotation 或全量历史删除。
