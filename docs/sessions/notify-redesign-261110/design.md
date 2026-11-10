# DESIGN-note：通知目标显式化——只发给任务声明的订阅者，目标随任务持久化，程序方改用长轮询

> 会话：notify-redesign-261110 · 风险等级 personal · 基线 `a880418b`
> 依据：主脑第一性原理草案 + consult（codex、claude-opus）两家独立只读核实；设计稿经 codex review 一轮修订（v2）。
> 用户已拍板：P0–P2 全做；user_005/user_008 停发全局群；callback 不做。

## 目标

每个租户的任务通知只发到**这个任务声明过的**目标（用户配置或请求里显式给的），不再静默落到运营者的全局群；
服务重启、补发、重新校对/总结/笔记都发到确定的一组目标；程序/agent 调用方可以选择不推 IM，并能用长轮询等任务结束。

## 非目标

- 不做通用 HTTP callback_url（触发判据见「已否决」）。
- 不做事件可见性矩阵、投递记录表、HMAC、用户级飞书签名 secret、新渠道。
- 不改 users.json 字段名、TranscribeRequest 已有字段名、skill 环境变量名。
- 不改审计页：它只展示/筛选提交时的企微字段（audit.py:552、625），**不是**实际投递清单，本批不改其语义。
- 不追求精确一次投递：部分渠道失败时整组重试，已成功的渠道可能重复收到（terminal_status.py:405），接受此语义。

## 为什么不是分区 / 删除 / 约定

- **分区**：把「运维告警」（ASR 监控，显式发全局）与「租户任务通知」（只发任务目标）拆成两条显式入口，租户通知不再共享全局目标。
- **删除**：删除三层按渠道独立的隐式回退、跨渠道失败改投、终态时从 users.json/审计表反查；callback_url 删出范围。
- **约定**：users.json「空 = 不发」作约定，不新增配置项；legacy 单 token 用户约定为运营者本人，保留全局目标。

## 方案要点

### C1 止血（P0）

- NotificationRouter：渠道启用不再取决于全局配置是否有 webhook；租户方法只向 `webhooks` 字典里有地址的渠道发，
  缺渠道静默跳过（仅当整个任务零目标时打一行 `NOTIFY_TARGET_MISSING task_id=…`，取代 router.py:68-88 逐渠道告警）。
- 删除跨渠道失败改投（router.py:113-127/161-178/211-228）。
- 渠道对象（FeishuChannel / WeComChannel / WechatNotifier 及 wechat.py 导出的辅助函数）发租户消息时不得用自身默认（全局）webhook 兜底
  （channel.py:267/288、wechat.py:88、wechat.py:374-525）。
- 删除 tasks.py 三处显式拼全局 webhook（约 813-823、1121-1131、1403-1412）及其写入审计的全局地址。
- **旧积压防护**：补发路径 `_resolve_delivery_webhooks`（terminal_status.py:536-557）在 C1 阶段暂留反查，但对**非 legacy 提交者**
  剔除等于全局配置 webhook 的地址（历史重跑已把全局地址写进审计，tasks.py:816、914）；C2 再整体删掉反查。
- 终态通知零目标 = 已处理（记 skipped），不进 0.5s 无限重试（terminal_status.py:337、cache_manager.py:3113）。
- 计数语义改名：渠道返回 True 只表示「接受入队」（wechat.py:208），非送达；任一**声明的**渠道未被接受即整体未接受，进入既有重试。
- 运维告警显式入口：ASR 监控改调显式发全局的方法（asr_monitor.py:252-255）。slow_alert 是租户通知，沿用任务目标。
- legacy_user 保留全局目标（user_manager.py:194/231 现状不变）。
- 启动时按用户打一行 `NOTIFY_PROFILE user_id=… channels=[…]`（不含地址）。

### C3 长轮询 + skill wait（P2a，与 C1 并行）

- `GET /api/task/{id}?wait=<秒,1–60>`：任务进终态或超时即返回，响应 schema 与无 wait 时一致；等待用异步方式，不阻塞事件循环。
- 一次 wait 请求只记一行审计（沿用每请求一行，tasks.py:565）。
- skill 新增 `wait` 子命令（skill/scripts/videotranscript.py），客户端 HTTP 超时必须大于 wait（videotranscript.py:36）。

### C2 目标落库 + 统一 resolver + notify:none（P1 + P2b，依赖 C1、C3 合并）

- 唯一纯函数 `resolve_notify_targets(user_info, request) -> dict[channel, webhook]`，五个入口共用：提交、上传、重新校对、重新总结、笔记
  （现分别在 tasks.py:319-338/812-825/1120-1133/1402-1415、uploads.py:153-162 各组装一次）。
- 优先级（与现代码一致，tasks.py:327-338）：`notification_config{channel,webhook}` > 顶层 `wechat_webhook`/`feishu_webhook` > 用户配置。
  - 只给 channel 不给 webhook = 「只发该渠道」（从已解析集合里筛）——**行为变更**，文档写明；channel 取值限 wechat/feishu。
  - 给 webhook 不给 channel = 422 拒绝（fail fast，不猜渠道）。
- `notify: "default" | "none"`（默认 default）；`none` 时 resolver 返回 `{}`，零 IM 发送（view link、偏慢提醒、终态正文、上传回执）。
  上传元数据白名单（uploads.py:34、54）与重跑请求模型（transcription.py:363、385，现仅有企微字段）补齐 `feishu_webhook`、`notify`。
- 重跑类（校对/总结/笔记）实际新建子任务（tasks.py:733、754）：定死为**按本次重跑请求 + 提交者当前用户配置重新解析**（它是一次新提交），不继承父任务目标。
- 任务表新增 `notify_targets` 列（JSON），提交事务内写入。语义定死：
  - `NULL` = 本特性上线前的旧行；`{}` = 显式零目标，永久静默；非空 dict = 唯一投递目标。
  - 建表、ALTER 迁移、表重建、读出解码四处都要覆盖（cache_manager.py:388、608、647、3631）。
- 终态/补发/偏慢提醒/缓存命中直接返回路径（transcription.py:1636）只读该列，删除 terminal_status.py:395、536-586 的 users.json + 审计表反查。
- 旧行兼容（`NULL`）：按提交者当前用户配置解析（不回退全局），打 `NOTIFY_TARGETS_LEGACY_ROW task_id=…`；
  提交者也为 NULL 的旧行 = 零目标、记 skipped。原请求级覆盖对旧行无法恢复，接受。
- skill 新增 `--no-notify`：传它时发 `notify:"none"` 且不注入环境变量 webhook；与 C3 的 `wait` 集成验证。

## 已否决方案

- callback_url：仓内无不能轮询的程序消费者证据（skill 只有 submit/status/result），出站任意 URL 带来 SSRF/签名/重试成本。
  触发判据：出现不能常驻轮询且需要跨小时推送的调用方，或轮询明显占用审计/负载。
- SSE：解决不了离线接收，长轮询已足够。
- 全局 webhook 继续兜底只加告警：今天的事故正是如此（PR #296 已加告警但未止泄漏）。
- 独立订阅表：8 用户个人项目，一列 JSON 足够。
- C2/C4 分两卡：两卡都要接同一批请求模型与 resolver，合并避免反复接线（codex review）。

## 关键不变式（测试一律断言最终 HTTP 边界实际请求的 URL，复用 tests 中 test_notification_e2e_delivery.py:88 的边界方式）

1. 非 legacy 租户任务的任何通知都不请求全局 webhook URL；**同时**声明的目标确实收到请求（正例，防「全禁发」也变绿）。覆盖 router 全部租户方法、`{}`、`{"feishu": x}`。— C1
2. 无全局飞书配置时，用户级飞书仍发出。— C1
3. 零目标终态通知只处理一次：跨多轮补发与重启都不重发、不重试。— C1
4. 声明的渠道未被接受 ≠ 成功：飞书失败、企微成功时任务不记为已接受。— C1
5. ASR 监控告警仍发全局 webhook。— C1
6. 补发路径对非 legacy 提交者剔除审计里记录的全局地址。— C1
7. 五个入口内联通知与重启后补发目标一致；提交后修改 users.json 不改变已提交任务目标；`{}` 行永久静默、`NULL` 行走兼容。— C2
8. `notify=none` 在五个入口下 view link、偏慢提醒、终态正文、上传回执零发送。— C2
9. 只给 webhook 不给 channel 返回 422。— C2
10. `wait` 在终态立即返回、超时返回当前状态，schema 一致；等待期间其他请求不被阻塞；skill 客户端超时 > wait。— C3

## 卡与依赖

| 卡 | 内容 | 依赖（具体产物） | 粗估 diff（含测试） |
|---|---|---|---|
| C1 | 止血 | 无 | 700–1100 |
| C3 | 长轮询 + skill wait | 无（独立 worktree；tasks.py 只碰 GET /api/task 处理函数，约 423-504 区外） | 350–650 |
| C2 | 目标落库 + resolver + notify:none + skill --no-notify | C1 合并后的 router 租户语义（空 = 不发）与 tasks.py 去全局拼接；C3 合并后的 skill `wait` 代码（同文件集成） | 1300–2200 |

## 上线注意

- C1 上线后 user_005、user_008 不再收到企微通知（用户已确认停发）；卿小璐（user_007）在 C1 上线后删除 wechat_webhook，只留飞书。
- 每张卡合并后按现有部署流程上线，部署后看启动日志 `NOTIFY_PROFILE` 行核对每个用户的渠道。
- C2 含 schema 迁移（新增可空列），上线前备份任务库。
