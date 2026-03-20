# API Quick Start

本文档帮助下游客户端快速接入视频转录 API。完整的参数定义和响应 Schema 请访问服务的 `/docs`（Swagger UI）页面。

---

## 认证

所有 API 请求需在 HTTP Header 中携带 Bearer Token：

```
Authorization: Bearer <your-api-key>
```

API Key 由服务管理员分配，格式通常为 `sk-xxx-xxxxxxxx`。如需获取，请联系管理员在 `config/users.json` 中为你创建账号。

---

## 核心流程

```
提交任务 ──→ 轮询状态 ──→ 获取结果
  POST          GET         GET
/api/transcribe  /api/task/{id}  /view/{token}
```

### 1. 提交转录任务

```bash
curl -X POST https://your-domain.com/api/transcribe \
  -H "Authorization: Bearer sk-xxx-xxxxxxxx" \
  -H "Content-Type: application/json" \
  -d '{
    "url": "https://www.youtube.com/watch?v=VIDEO_ID",
    "use_speaker_recognition": true
  }'
```

**请求参数**：

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `url` | string | 是 | 视频/音频平台链接 |
| `use_speaker_recognition` | bool | 否 | 是否启用说话人识别，默认 `false` |
| `download_url` | string | 否 | 自定义下载地址（跳过平台解析） |
| `wechat_webhook` | string | 否 | 企业微信 Webhook，完成后推送通知 |
| `metadata_override` | object | 否 | 覆盖元数据，见下方说明 |

`metadata_override` 字段：

| 子参数 | 类型 | 说明 |
|--------|------|------|
| `title` | string | 自定义标题 |
| `author` | string | 自定义作者 |
| `description` | string | 自定义描述 |

**响应示例**（HTTP 202）：

```json
{
  "code": 202,
  "message": "任务已提交",
  "data": {
    "task_id": "task_abc123...",
    "view_token": "view_xyz789..."
  }
}
```

**支持的平台**：YouTube、Bilibili、小宇宙播客、小红书、抖音。其他链接会尝试通用下载。

### 2. 轮询任务状态

```bash
curl https://your-domain.com/api/task/task_abc123... \
  -H "Authorization: Bearer sk-xxx-xxxxxxxx"
```

**响应状态码含义**：

| HTTP Code | `data.status` | 含义 | 客户端动作 |
|-----------|---------------|------|------------|
| 202 | `queued` | 排队中 | 继续轮询 |
| 202 | `processing` | 处理中 | 继续轮询 |
| 200 | `success` | 完成 | 获取结果 |
| 500 | `failed` | 失败 | 读取错误信息 |

**推荐轮询策略**：每 5-10 秒请求一次，设置最大超时（建议 30 分钟，长音频处理耗时较久）。

### 3. 获取结果

任务完成后，通过 `view_token` 获取结果，有以下方式：

```bash
# 网页查看（浏览器打开）
https://your-domain.com/view/{view_token}

# 获取校对文本（纯文本）
https://your-domain.com/view/{view_token}?raw=calibrated

# 获取内容总结（纯文本）
https://your-domain.com/view/{view_token}?raw=summary

# 获取原始转录（纯文本）
https://your-domain.com/view/{view_token}?raw=transcript
```

> `view_token` 链接无需认证，可直接分享。

---

## 完整示例（Python）

```python
import requests
import time

BASE_URL = "https://your-domain.com"
API_KEY = "sk-xxx-xxxxxxxx"
HEADERS = {
    "Authorization": f"Bearer {API_KEY}",
    "Content-Type": "application/json",
}

# 1. 提交任务
resp = requests.post(f"{BASE_URL}/api/transcribe", headers=HEADERS, json={
    "url": "https://www.xiaoyuzhoufm.com/episode/xxxxx",
    "use_speaker_recognition": True,
})
data = resp.json()["data"]
task_id = data["task_id"]
view_token = data["view_token"]
print(f"Task submitted: {task_id}")

# 2. 轮询状态
while True:
    resp = requests.get(f"{BASE_URL}/api/task/{task_id}", headers=HEADERS)
    result = resp.json()
    status = result.get("data", {}).get("status", "unknown")
    print(f"Status: {status}")

    if status == "success":
        break
    elif status == "failed":
        print(f"Error: {result.get('message')}")
        break

    time.sleep(10)

# 3. 获取结果
calibrated = requests.get(f"{BASE_URL}/view/{view_token}?raw=calibrated").text
print(f"Result ({len(calibrated)} chars):\n{calibrated[:500]}")
```

---

## 补充说明

- **交互式 API 文档**：访问 `https://your-domain.com/docs` 查看完整的 Swagger UI，可在线调试。
- **企微通知**：设置 `wechat_webhook` 后，任务完成会自动推送校对结果和查看链接到企业微信群。
- **错误处理**：所有 API 错误以统一格式返回 `{"code": <http_code>, "message": "<错误描述>", "data": null}`。
- **并发限制**：服务端有任务队列，队列满时返回 HTTP 503，客户端应稍后重试。
