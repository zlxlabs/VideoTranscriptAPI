# Stacked PR 拆分合并与生产部署记录（260721）

日期：2026-07-20 ~ 2026-07-21 ｜ 状态：已完成

## 一、Stacked PR 拆分与合并

feat/chapters-wiring（109 提交、约 2.4 万行 patch）按 ≤4000 行/个拆分为 stacked PR，
逐个走 gate CI（lint + 全量测试 + agent review），全部通过后按栈序合并进 main：

| PR | 内容 | 通过方式 |
|---|---|---|
| #15 | T3 segments 适配器 | 1 轮修复循环（cherry-pick 历史修复 + 全链 rebase） |
| #16 | T2 字幕时间戳 + T5 ChaptersProcessor | rerun 解 placeholder 后真实 pass |
| #17 | gate 加固 r1–r14 | rerun 追到真实 pass（首轮 placeholder 假 pass） |
| #18 | gate 加固 r15–r28 | 审计豁免（2 轮干净内容被误判 fail） |
| #19 | 缓存层 timeline 读写 | 修复 4 条真 major 后豁免（复审内容全过） |
| #20 | 协调器/llm_ops 接线 | 修复 3 条 major 后真实 pass |
| #22 | T8 全链 + T9/T10（合并原 #21，消除 dead-code 拆分） | 真实 pass |
| #23 | T11 章节 UI 重设计 | 首轮真实 pass |
| #24 | features 回归测试夹具修复 | 真实 pass |
| #25 | 静态资源版本指纹 + no-cache | 真实 pass |

合并方式：merge commit 保提交 SHA，逐个 retarget 下一 PR 至 main；临时分支全部清理。
main 从 1bc6bc3 推进到 ddb4ce6。

## 二、评审抓出并已修复的真 bug（均带回归测试）

1. 缓存覆盖混用新正文+旧 timeline（侧车残留）
2. 空 text+有 segments 的字幕被当空正文写缓存送 LLM
3. CapsWriter None 时间算术 TypeError 致侧车整体生成失败
4. 倒挂时间区间生成递减时间轴
5. recalibrate 对任何旧状态无条件重算章节
6. 无 speaker 结构化 dialogs 渲染 KeyError
7. （栈内时序）pytest 导入缺失

## 三、gate 评审基建问题

claude-glm verdict 可靠性故障 6 次（placeholder×4 含两次假 pass、内容与 verdict
矛盾×2、撤回性 finding×1）。证据与修复建议（validate_verdict 一致性校验 +
判无效走 failover）见同目录 **GATE-REVIEW-ISSUES.md**，待在 gate-hub 落地。

## 四、生产部署（n305）

- 镜像：`ghcr.io/zj1123581321/video-transcript-api:7cf5b98df862` → `:ddb4ce61d2fd`
- 流程：`docker/push_to_ghcr.sh`（干净 worktree 构建）→ n305 `pull_and_deploy.sh`
  （预检/候选/健康检查/自动回滚）
- 生产 config 变更：`llm.min_chapters_threshold: 1000`（备份 config.jsonc.bak-prechapters）
- DB 迁移自动完成（chapters_status、审计快照 v5）
- 生产端到端验证：部署后首个任务（bilibili《老储走甘肃》）全链路成功，
  14 章全部 jump_ok，内嵌章节头与数据岛正常

## 五、Cloudflare 边缘缓存事故与修复

症状：部署新版后用户端章节目录不出现；页面 HTML 是新版（有内嵌章节头）但
floating-toc.js 是旧版，强刷+无痕均无效。根因：用户经 **Cloudflare Tunnel**
公网访问，CF 边缘按扩展名缓存静态资源（HTML 不缓存、JS 缓存），缓存不在任何
终端设备上。修复（PR #25）：

- 静态文件内容哈希注入模板（`floating-toc.js?v=<sha256前8位>`），内容变 URL 必变
- `/static/` 响应加 `Cache-Control: no-cache`（回源验证 + ETag 304）

**访问方式备忘**：生产服务在 n305（Tailscale `100.68.21.80:8200`，容器 8200→8000），
用户日常经 Cloudflare Tunnel 公网域名访问；旧入口 `100.87.124.57:8010` 已废弃（无监听）。

## 六、遗留 backlog 索引

- 评审 minor/nit：见各 PR 评论与 GATE-REVIEW-ISSUES.md 末尾汇总
- T11 UI backlog：见 REVIEW-LOG.md T11 节
- gate 一致性校验修复：待 gate-hub 立项
