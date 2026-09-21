# 多渠道通知配置指南

VideoTranscriptAPI 支持同时向企业微信和飞书推送任务通知。基于 [wecom-notifier](https://github.com/zj1123581321/wecom-notifier) v0.3.1+。

## 架构

```
  API 请求 / 转录任务 / ASR 监控
       │
       ▼
  NotificationRouter（全局单例）
       │  按配置分发到所有启用的渠道
       ├──────────────────┐
       ▼                  ▼
  WeComChannel         FeishuChannel
  (企业微信 markdown)   (飞书卡片消息)
```

## 快速开始

### 1. 全局配置

在 `config/config.jsonc` 中添加渠道 webhook：

```jsonc
// 企业微信（配了 webhook 就自动启用）
"wechat": {
    "webhook": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=YOUR-KEY"
},

// 飞书（配了 webhook 就自动启用）
"feishu": {
    "webhook": "https://open.feishu.cn/open-apis/bot/v2/hook/YOUR-KEY",
    "secret": ""  // 可选，机器人启用签名校验时填写
}
```

**规则：有 webhook 就启用，没配就不启用。两个都配了 = 同时推送两个渠道。**

### 2. 用户级配置

在 `config/users.json` 中为每个用户配置独立的 webhook：

```json
{
  "users": {
    "sk-user001-xxx": {
      "user_id": "user_001",
      "name": "张三",
      "wechat_webhook": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=user1-key",
      "feishu_webhook": "https://open.feishu.cn/open-apis/bot/v2/hook/user1-key",
      "enabled": true
    }
  }
}
```

用户级 webhook 优先于全局配置。用户 A 的通知发到用户 A 的群，不会和全局群混。

### 3. Per-request 指定渠道

API 请求可以指定特定渠道：

```bash
# 只发到飞书
curl -X POST "http://localhost:8000/api/transcribe" \
  -H "Authorization: Bearer your-token" \
  -H "Content-Type: application/json" \
  -d '{
    "url": "https://www.youtube.com/watch?v=xxx",
    "notification_config": {
      "channel": "feishu",
      "webhook": "https://open.feishu.cn/open-apis/bot/v2/hook/custom-key"
    }
  }'
```

也可以用旧的 `wechat_webhook` 字段（向后兼容）：

```bash
curl -X POST "http://localhost:8000/api/transcribe" \
  -d '{"url": "...", "wechat_webhook": "https://qyapi.weixin.qq.com/..."}'
```

## Webhook 优先级

```
notification_config.webhook  (per-request 指定)
        ↓ 未设置时
wechat_webhook 字段          (per-request 向后兼容)
        ↓ 未设置时
user_info.wechat/feishu_webhook  (用户级配置)
        ↓ 未设置时
config.wechat/feishu.webhook     (全局配置)
```

每个渠道独立解析，互不影响。

## Fallback 机制

当指定渠道发送失败时，自动退到其他可用渠道：

- 指定飞书但飞书 webhook 失败 → 自动退到企业微信
- 指定企业微信但企微 webhook 失败 → 自动退到飞书
- 不指定渠道（全部推送）时，各渠道独立发送，互不影响

## 通知时机

| 阶段 | 通知内容 |
|------|---------|
| 任务创建 | 查看链接（view URL） |
| 开始处理（进行中） | 转录引擎信息；文案带「进行中」，不是终态 |
| 缓存命中 | 标题、作者、转录预览 |
| 转录完成（进行中） | 中间态。文案含「后面还有校对/摘要」，避免被当成任务已结束 |
| LLM 完成 / 终态 success | 内容正文（总结/校对/笔记）先入队，随后状态通知 `【任务完成】` |
| 终态 failed | 状态通知 `【任务失败】`（含错误与查看链接） |
| ASR 告警 | 服务宕机/恢复通知 |

中间态与终态必须能区分：用户把「转录完成」当成终态来等，是 2026-09-21 n305 事故的一部分。终态状态通知只由 `finalize_terminal_status_and_notify` / outbox 出口产生，worker 只发内容正文。

## 终态状态通知 outbox

任务写入 `success` / `failed` 且 CAS 获胜时，在同一 SQLite 事务里向 `task_terminal_notifications` 插入一行 pending（`task_id` UNIQUE）。投递线程启动时先扫全部 `notified_at IS NULL` 的行补发，文案带原始 `completed_at`，避免和刚提交的任务混在一起。

两态 `pending → sent`，不做 `sending`。投递由**单线程串行**完成：服务是单进程（uvicorn 无 `workers`、单容器单副本），投递线程是该表唯一的写者，互斥由「同一线程逐行处理」天然提供。**没有 owner 锁、没有接管租约、没有 claim/release**：这些机制仲裁的并发在本部署拓扑下不存在，且多进程场景下任务状态机本身早已崩坏（启动时无条件把所有非终态行写 failed），给通知表加锁救不了任何东西。`attempts` 仅作观测计数（每次投递尝试递增），**不参与任何过滤**。

`sent`（= `notified_at`）**只表示「已提交给投递器」**，不表示送达：当前实现下投递器 = 进程内 daemon 队列（`async_send=True` 立即返回）。`notified_at` 只在发送未抛异常、且路由至少一个渠道返回 True 时写入；发送抛异常或渠道全 False 不标 sent，该行**保持列出**并在下一轮重试。

这是**有界至少一次**：崩溃或失败窗口内**可能重复一条**终态通知，但不会静默丢失。漏发比重复更糟。

### 不变式（review 的规格，逐条可证伪）

- **I1 原子**：任一次终态 CAS 获胜 ⇔ 同事务内 `task_terminal_notifications` 出现该 task_id 一行；CAS 落败 ⇒ 不出现行。
- **I2 唯一**：`task_id` UNIQUE，同一任务至多一行（由 schema 保证）。
- **I3 一致**：该行携带的状态 = 当次 CAS 写入的终态；文案由该行渲染，不重新查任务状态决定「是否该发」。
- **I4 不丢**：进程启动时与每个投递周期，所有 `notified_at IS NULL` 的行必须被尝试投递；**不存在任何条件使某一行永久不被列出**。
- **I5 至少一次**：`notified_at` 只在一次**返回真实成功信号**的发送之后写入；崩溃于此之间 ⇒ 允许重复一条（显式接受的窗口）。
- **I6 抑制在写入期决定**：需要抑制通知的终态写入路径，**在同一事务里就不产生可投递的行**（`update_task_status(suppress_terminal_notification=True)`，HTTP 建行后清理走这条）。

投递器只接受路由返回的渠道结果字典，并要求至少一个渠道值为真；`None`、布尔值、模拟对象或渠道全 False 都不会标记 sent。

超过 3 次仍失败的行**不停止补发**，只多记一条 warning 日志（`STUCK_ATTEMPT_WARN_THRESHOLD`）：任何让一行同时满足「未送达」且「不再被任何视图列出」的机制，都是本卡要消灭的那类静默事故。

outbox 不做限流/重试/分段：`wecom-notifier` 是唯一限流权威。outbox 只承载终态**状态**通知（状态行 + 错误 + 查看链接），不把总结/校对/笔记正文搬进表。

恢复路径没有请求上下文，目标解析顺序：

1. `api_audit_logs.wechat_webhook` by task_id
2. `user_manager.get_user_by_id(submitted_by)` 的用户级 webhook
3. 全局配置默认渠道

飞书自定义 webhook 未落库，恢复路径取不回，是已知缺口。

### 投递侧残留风险（本卡不修）

`utils/notifications/wechat.py` 里 `async_send=True` 立即返回，表示已提交到 wecom-notifier 内部队列，不代表已送达。`shutdown_global_notifier()` 不 flush。关闭预算耗尽时 `shutdown_all_notifiers()` 可能被跳过。真实 SIGKILL 窗口：若进程在 wecom-notifier 已接收但 `notified_at` 尚未写入时被杀，重启会按 attempts 再补发（可能重复一条）。覆盖不了 wecom-notifier 内部队列未 flush 的毫秒级窗口。

## 消息格式

- **企业微信**：markdown_v2 格式，超长文本自动分段
- **飞书**：卡片消息（Markdown 内容），自动分段，支持模板颜色

两个平台接收到的消息内容相同，格式自动适配。

### 校对/总结状态文案（诚实状态模型）

若任务通过 `processing_options` 关闭了校对或总结（见[处理深度开关功能文档](../features/processing_options.md)），"LLM 完成"通知的转录统计行会体现真实状态，不再统一显示"未生成"：

| 总结状态 | 通知文案 |
|---|---|
| 生成失败（`failed`） | "生成失败" |
| 主动关闭（`disabled`） | "未启用" |
| 其他（未生成/处理中） | "未生成" |

校对质量异常（`partial`/`none`）时，通知正文会额外附带一段 `⚠️ 校准部分异常` / `⚠️ 校准完全失败` 警告文案（`api/services/llm_ops.py::_build_calibration_warning()`）。

校对被 `processing_options.calibrate=False` 主动关闭（`calibration_status=disabled`）时同样会附带提示：`⚠️ AI 校对未启用：当前显示为未经校对的原始语音识别文本（可能含错别字、断句错误）`——避免 `calibrate=False, summarize=True` 场景下用户把"基于未校对原文生成的总结"误当成正常校对结果查看。首次处理与"缓存全命中，直接复用历史结果"两条通知路径都会带上这条提示（后者见 `api/services/transcription.py` 中缓存命中分支）。

## 相关文件

| 文件 | 说明 |
|------|------|
| `utils/notifications/router.py` | NotificationRouter 路由层 |
| `utils/notifications/channel.py` | Channel 协议 + WeComChannel + FeishuChannel |
| `utils/notifications/wechat.py` | WechatNotifier（保留，向后兼容） |
| `utils/notifications/__init__.py` | 全局生命周期管理 |
| `config/config.example.jsonc` | 配置示例（含飞书段） |
| `config/users.example.json` | 用户配置示例（含 feishu_webhook） |

## wecom-notifier 库文档

底层通知库的详细 API 参考：[企业微信通知器使用指南](wechat_notification.md)
