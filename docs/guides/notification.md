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
| 开始处理 | 转录引擎信息 |
| 缓存命中 | 标题、作者、转录预览 |
| 转录完成 | 状态更新 |
| LLM 完成 | 总结/校对文本 + 查看链接 |
| 任务失败 | 错误信息 |
| ASR 告警 | 服务宕机/恢复通知 |

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
