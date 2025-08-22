# 多用户鉴权系统配置指南

本文档介绍如何配置和使用视频转录API的多用户鉴权系统。

## 功能概述

多用户鉴权系统提供以下功能：

1. **多用户支持**：每个用户拥有独立的API密钥
2. **用户级配置**：每个用户可以配置自己的企业微信webhook地址
3. **使用统计**：记录每个用户的API调用情况
4. **向下兼容**：支持原有的单token配置方式

## 配置步骤

### 1. 创建用户配置文件

在 `config/` 目录下创建 `users.json` 文件：

```json
{
  "users": {
    "sk-user001-xxxxxxxxxx": {
      "user_id": "user_001",
      "name": "张三",
      "wechat_webhook": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=user1-key",
      "created_at": "2025-01-01T00:00:00Z",
      "enabled": true
    },
    "sk-user002-yyyyyyyyyy": {
      "user_id": "user_002", 
      "name": "李四",
      "wechat_webhook": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=user2-key",
      "created_at": "2025-01-01T00:00:00Z",
      "enabled": true
    }
  }
}
```

### 2. 字段说明

- **API密钥**（作为key）：用户的唯一标识符，建议格式：`sk-userXXX-随机字符串`
- **user_id**：用户内部ID，用于日志和统计
- **name**：用户显示名称
- **wechat_webhook**：用户专用的企业微信webhook地址（可选）
- **created_at**：用户创建时间
- **enabled**：用户是否启用（false时该用户无法使用API）

### 3. API密钥生成建议

推荐使用以下格式生成API密钥：
```
sk-{user_identifier}-{random_string}
```

示例：
- `sk-zhang001-a1b2c3d4e5f6g7h8`
- `sk-li002-x9y8z7w6v5u4t3s2`

## 使用方式

### 1. API调用

使用标准的Bearer Token认证方式：

```bash
curl -X POST "http://localhost:8000/api/transcribe" \
  -H "Authorization: Bearer sk-user001-xxxxxxxxxx" \
  -H "Content-Type: application/json" \
  -d '{"url":"https://example.com/video.mp4"}'
```

### 2. 企业微信通知优先级

系统按以下优先级确定webhook地址：
1. 请求参数中的 `wechat_webhook`
2. 用户配置中的 `wechat_webhook`
3. 全局配置中的 `wechat_webhook`

### 3. 新增API端点

#### 获取用户统计信息
```bash
curl -X GET "http://localhost:8000/api/audit/stats?days=30" \
  -H "Authorization: Bearer sk-user001-xxxxxxxxxx"
```

#### 获取用户调用记录
```bash
curl -X GET "http://localhost:8000/api/audit/calls?limit=100" \
  -H "Authorization: Bearer sk-user001-xxxxxxxxxx"
```

#### 获取用户配置信息
```bash
curl -X GET "http://localhost:8000/api/users/profile" \
  -H "Authorization: Bearer sk-user001-xxxxxxxxxx"
```

## 向下兼容

如果没有创建 `users.json` 文件，系统会自动回退到单token模式，使用 `config.json` 中的 `auth_token` 配置。

## 安全考虑

1. **API密钥保护**：
   - 使用HTTPS传输
   - 定期更换API密钥
   - 不要在日志中记录完整的API密钥

2. **访问控制**：
   - 用户只能查看自己的统计信息
   - 用户只能查看自己的调用记录

3. **审计日志**：
   - 所有API调用都会记录到审计日志
   - 包含时间戳、用户ID、端点、处理时间等信息

## 数据存储

- **用户配置**：存储在 `config/users.json` 文件中
- **审计日志**：存储在 `data/audit.db` SQLite数据库中

## 故障排除

### 1. 用户无法认证

检查：
- `users.json` 文件格式是否正确
- 用户的 `enabled` 字段是否为 `true`
- API密钥是否正确

### 2. 企业微信通知不工作

检查：
- 用户配置中的 `wechat_webhook` 是否正确
- 请求参数中是否有冲突的webhook设置

### 3. 统计信息不准确

检查：
- `data/audit.db` 文件是否存在
- 数据库是否有写入权限

## 配置示例

完整的多用户配置示例：

```json
{
  "users": {
    "sk-admin-1234567890abcdef": {
      "user_id": "admin",
      "name": "系统管理员",
      "wechat_webhook": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=admin-key",
      "created_at": "2025-01-01T00:00:00Z",
      "enabled": true
    },
    "sk-user1-abcdef1234567890": {
      "user_id": "user_001",
      "name": "普通用户1",
      "wechat_webhook": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=user1-key",
      "created_at": "2025-01-15T08:30:00Z",
      "enabled": true
    },
    "sk-user2-fedcba0987654321": {
      "user_id": "user_002",
      "name": "普通用户2",
      "wechat_webhook": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=user2-key",
      "created_at": "2025-01-20T14:15:00Z",
      "enabled": true
    }
  }
}
```