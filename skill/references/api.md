# VideoTranscriptAPI 端点参考

只有当 CLI 无法满足，或需要排查异常响应、使用未暴露的字段时才读这份文档。否则优先走 `scripts/videotranscript.py`。

## 鉴权

所有 `/api/*` 端点需要：

```
Authorization: Bearer <token>
```

token 可能是 `config.jsonc` 中 `api.auth_token`（单用户），或 `config/users.json` 中某个用户的 `api_key`（多用户）。

`/view/*`、`/export/*`、`/health` 无需鉴权。

## 端点清单

| 端点 | 方法 | 鉴权 | 说明 |
|------|------|------|------|
| `/api/transcribe` | POST | ✓ | 提交转录任务 |
| `/api/task/{task_id}` | GET | ✓ | 查询任务状态 |
| `/api/recalibrate` | POST | ✓（需权限） | 触发重新校对 |
| `/api/audit/stats` | GET | ✓ | 调用统计（默认近 30 天） |
| `/api/audit/calls` | GET | ✓ | 最近调用记录 |
| `/api/audit/history` | GET | ✓ | 历史任务过滤查询 |
| `/api/audit/summary` | GET | ✓ | 任务摘要预览（前 300 字） |
| `/api/audit/filter-options` | GET | ✓ | 获取可选的过滤值 |
| `/api/users/profile` | GET | ✓ | 当前用户信息 |
| `/view/{view_token}` | GET | — | HTML 查看页 / 带 `?raw=` 导出纯文本 |
| `/export/{view_token}/{type}` | GET | — | 文件下载（带 YAML front matter） |
| `/health` | GET | — | 服务健康检查 |

## POST /api/transcribe

**Request body**

```json
{
  "url": "https://...",                        // 必填
  "use_speaker_recognition": false,            // 可选，默认 false
  "wechat_webhook": "https://qyapi...",        // 可选，覆盖服务端默认
  "download_url": "https://direct-link.mp4",   // 可选，绕过平台解析
  "metadata_override": {                       // 可选
    "title": "...",        // ≤ 200 字
    "author": "...",       // ≤ 200 字
    "description": "..."   // ≤ 2000 字
  }
}
```

**Response 202**

```json
{
  "code": 202,
  "message": "任务已提交",
  "data": {
    "task_id": "task_xxx",
    "view_token": "vt_yyy"
  }
}
```

**典型错误**

| HTTP | 场景 | 处理 |
|------|------|------|
| 400 | `url` 为空或格式不对 | 修正请求体 |
| 401 | 无/错误 Authorization header | 检查 token |
| 503 | 队列已满 | 退避 1–2 分钟重试 |

## GET /api/task/{task_id}

**HTTP code 对应任务状态**

| HTTP | `data.status` | 含义 |
|------|--------------|------|
| 202 | `queued` \| `processing` | 还在队列里或处理中 |
| 200 | `success` | 完成，`data` 含结果字段 |
| 500 | `failed` | 失败，`data.error` 含原因 |
| 404 | — | task_id 不存在 |

**`status=success` 时的响应**

```json
{
  "code": 200,
  "data": {
    "status": "success",
    "view_token": "vt_yyy",
    "video_title": "...",
    "author": "...",
    "transcript": "...",          // 转录正文
    "cached": false,              // 是否命中缓存
    "speaker_recognition": false
  }
}
```

## GET /view/{view_token}

三种查询参数，返回类型不同：

| Query | Content-Type | 用途 |
|-------|--------------|------|
| 无 | `text/html` | 交互式查看页 |
| `?raw=calibrated\|summary\|transcript` | `text/plain; charset=utf-8` | 纯文本（推荐给 agent） |
| `?page=calibrated\|summary\|transcript` | `text/html` | 爬虫友好 HTML |

**HTTP code**

| HTTP | 含义 |
|------|------|
| 200 | 正常返回 |
| 202 | 任务仍在处理 |
| 404 | view_token 无效 |
| 410 | 结果已清理 |

**`type` 说明**

- `summary`：LLM 生成的总结（只有长文本任务会生成）
- `calibrated`：LLM 校对过的完整文字稿
- `transcript`：原始 ASR 输出（FunASR JSON 或 CapsWriter 纯文本）

## GET /export/{view_token}/{type}

同 `?raw=` 但 Content-Disposition 为 `inline`，文件名形如 `{title}-{type}-{platform}.txt`，开头带 YAML front matter（title/author/platform/url）。

## GET /api/audit/history

**Query params**

| 参数 | 说明 |
|------|------|
| `start_date` / `end_date` | `YYYY-MM-DD` |
| `platform` | `youtube` / `bilibili` / `douyin` / `xiaohongshu` / `xiaoyuzhou` |
| `author` | 频道/作者名（支持逗号分隔多选） |
| `status` | `success` / `failed` / `processing` |
| `webhook` | webhook URL |
| `q` | 关键词（命中标题/作者/内容） |
| `limit` | 默认 20，上限 10000 |
| `offset` | 分页偏移 |

**Response**

```json
{
  "code": 200,
  "data": {
    "total": 123,
    "limit": 20,
    "offset": 0,
    "api_key_masked": "sk-xxx***",
    "items": [
      {
        "task_id": "...",
        "view_token": "...",
        "title": "...",
        "author": "...",
        "platform": "bilibili",
        "status": "success",
        "request_time": "2026-04-20 12:34:56",
        "video_url": "https://...",
        "wechat_webhook": "..."
      }
    ]
  }
}
```

注意：字段名是 `items`（不是 `tasks`），时间字段是 `request_time` 且为本地时间字符串。

## GET /api/audit/filter-options

返回可选的平台/作者/webhook 集合，用于 UI 下拉或参数校验：

```json
{
  "code": 200,
  "data": {
    "platforms": ["youtube", "bilibili", ...],
    "authors": [...],
    "webhooks": [...]
  }
}
```

## GET /api/audit/summary

Query：`view_token=...`。返回摘要前 300 字（快速预览用）。

## GET /api/users/profile

```json
{
  "code": 200,
  "data": {
    "user_id": "...",
    "api_key_masked": "sk-xxx***",
    "wechat_webhook": "...",
    "permissions": ["recalibrate", ...],
    "multi_user_mode": true
  }
}
```

## GET /health

无鉴权，排查连通性用：

```json
{
  "status": "healthy",        // 或 "degraded"
  "checks": {
    "sqlite": "ok",
    "capswriter": "ok",
    "funasr": "ok",
    "disk": "ok"
  }
}
```

## 错误响应通用结构

```json
{
  "code": 4xx|5xx,
  "message": "错误描述",
  "data": null
}
```

## 裸 HTTP Fallback（无 Python 时）

```bash
# 提交
curl -X POST "$BASE/api/transcribe" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"url":"https://www.bilibili.com/video/BVxxx"}'

# 查进度
curl -s -H "Authorization: Bearer $TOKEN" "$BASE/api/task/$TASK_ID"

# 拉总结（不需要 token）
curl -s "$BASE/view/$VIEW_TOKEN?raw=summary"
```
