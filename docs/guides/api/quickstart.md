# API Quick Start

本文档帮助下游客户端快速接入视频转录 API。完整的参数定义和响应 Schema 请访问服务的 `/docs`（Swagger UI）页面。

---

## 认证

除分享类接口（`/view/...`、`/export/...`）外，所有 API 请求需在 HTTP Header 中携带 Bearer Token：

```
Authorization: Bearer <your-api-key>
```

API Key 由服务管理员分配，格式为 `sk-<user>-xxxxxxxxxx`（在 `config/users.json` 中配置）。如需获取，请联系管理员创建账号。

> 单用户模式下，Key 即 `config/config.jsonc` 里的 `api.auth_token`；多用户模式下每个 Key 可绑定独立的企微 / 飞书 webhook。

---

## 统一响应格式

所有 JSON 接口返回统一信封。查询任务状态时，`data.status` 给出**显式状态字符串**，`code` / HTTP 状态码与之一致（两者皆可用于判断）：

```json
{
  "code": 202,
  "message": "任务处理中",
  "data": { "status": "processing", ... }
}
```

错误同样走这个信封：`{"code": <http_code>, "message": "<错误描述>", "data": null}`。

---

## 核心流程

```
提交任务 ──→ 轮询状态 ──→ 拉取结果
  POST          GET            GET
/api/transcribe  /api/task/{id}  /view/{token}?raw=calibrated
```

任务状态机:

```
queued ──► processing ──► calibrating ──► success
  (排队)      (下载+转录)    (LLM校对/总结)   (全部完成)
                                          └──► failed (任一阶段失败)
```

> ✅ **状态语义**：`/api/task` 返回 `success`(HTTP 200) 表示**全流程已完成**（含 LLM 校对/总结，产物已落盘），此时 `?raw=calibrated` 可直接取到内容。
> `calibrating` 表示转录已完成、校对/总结仍在进行（仍返回 202，继续轮询）。
> 状态以服务端数据库为准，**服务重启不丢**；崩溃中断的任务会在重启时被标记为 `failed`。
> 如果只想被动接收结果，配置 webhook 即可——任务全部完成后会推送「任务完成」消息和查看链接。

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
| `url` | string | 是 | 视频/音频平台链接，同时用于元数据解析、缓存去重和生成 view_token |
| `use_speaker_recognition` | bool | 否 | 是否启用说话人识别（FunASR），默认 `false`（普通转录走 CapsWriter） |
| `download_url` | string | 否 | 自定义直链下载地址；提供后跳过平台解析，`url` 仅用于取元数据 |
| `wechat_webhook` | string | 否 | 本次任务的企业微信 Webhook，覆盖用户级配置 |
| `feishu_webhook` | string | 否 | 本次任务的飞书 Webhook，覆盖用户级配置 |
| `notification_config` | object | 否 | 通知渠道配置，见下方 |
| `metadata_override` | object | 否 | 覆盖/补充元数据，见下方 |

`notification_config` 字段（用于按单次请求指定推送渠道）：

| 子参数 | 类型 | 说明 |
|--------|------|------|
| `channel` | string | 渠道：`wechat` / `feishu` / 不填（默认全部已配置渠道） |
| `webhook` | string | 自定义 webhook URL（写入对应 channel） |

`metadata_override` 字段（平台解析失败时作为覆盖，解析成功时作为补充）：

| 子参数 | 类型 | 说明 |
|--------|------|------|
| `title` | string | 自定义标题（≤200 字） |
| `author` | string | 自定义作者（≤200 字） |
| `description` | string | 自定义描述（≤2000 字） |

> webhook URL 会做 SSRF 安全校验，内网 / 非法地址会被拒绝（HTTP 422）。

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

**支持的平台**：YouTube、Bilibili、抖音、小红书、小宇宙播客。其他链接（或带 `download_url` 的直链）走通用下载。命中缓存时会直接复用历史结果。

### 2. 轮询任务状态

```bash
curl https://your-domain.com/api/task/task_abc123... \
  -H "Authorization: Bearer sk-xxx-xxxxxxxx"
```

**响应示例**（处理中）：

```json
{
  "code": 202,
  "message": "转录完成，校对/总结生成中",
  "data": {
    "status": "calibrating",
    "view_token": "view_xyz789...",
    "title": "示例标题",
    "author": "示例作者",
    "platform": "youtube",
    "completed_at": null
  }
}
```

状态由 `data.status` 显式给出，`code` / HTTP 状态码与之对应：

| `data.status` | HTTP Code | 含义 | 客户端动作 |
|---------------|-----------|------|------------|
| `queued` | 202 | 排队中 | 继续轮询 |
| `processing` | 202 | 下载 + 转录中 | 继续轮询 |
| `calibrating` | 202 | 转录完成，LLM 校对/总结中 | 继续轮询 |
| `success` | 200 | **全流程完成**，内容已就绪 | 拉取结果（步骤 3） |
| `failed` | 500 | 失败 | 读取 `data.error` 错误详情 |
| （不存在） | 404 | task_id 无效 | 停止 |

`status=failed` 时 `data.error` 给出失败原因（如 `"LLM处理失败: ..."`）。

**推荐轮询策略**：每 5–10 秒一次，设置最大超时（建议 30 分钟，长音频耗时较久）。状态以服务端数据库为准，服务重启不丢；崩溃中断的任务重启后会被置为 `failed`。

### 3. 获取结果

任务完成后通过 `view_token` 获取结果，**无需认证，可直接分享**：

```bash
# 网页查看（浏览器打开）
https://your-domain.com/view/{view_token}

# 纯文本：校对文本 / 内容总结 / 原始转录
https://your-domain.com/view/{view_token}?raw=calibrated
https://your-domain.com/view/{view_token}?raw=summary
https://your-domain.com/view/{view_token}?raw=transcript

# 爬虫友好的 HTML 页面（带 OG 标签，Markdown 渲染）
https://your-domain.com/view/{view_token}?page=calibrated

# 带文件名的下载（Content-Disposition）
https://your-domain.com/export/{view_token}/{type}   # type: calibrated/summary/transcript
```

当 `/api/task` 已返回 `success` 后，`?raw=calibrated` 直接返回 **200** 和正文。各状态码：

| HTTP Code | 含义 | 客户端动作 |
|-----------|------|------------|
| 200 | 内容就绪，响应体即正文（顶部含 YAML front matter 元数据） | 取用结果 |
| 202 | 任务仍在处理（未到 success 就提前访问时） | 等 `/api/task` 到 success |
| 404 | 该类型文件不存在（如未启用总结 / 说话人识别） | 放弃该类型 |
| 410 | 文件已被清理 | 需重新提交任务 |

> 纯文本响应顶部带 `---` 包裹的元数据头（Title / Platform / Type / Source / Export-Date），并通过 `X-Document-Title`、`X-Platform` 等响应头透出（非 ASCII 用 RFC 5987 编码）。

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
resp.raise_for_status()
data = resp.json()["data"]
task_id = data["task_id"]
view_token = data["view_token"]
print(f"Task submitted: {task_id}")

# 2. 轮询 data.status 直到 success（success 即表示全流程完成，含 LLM 校对/总结）
deadline = time.time() + 30 * 60
while time.time() < deadline:
    data = requests.get(f"{BASE_URL}/api/task/{task_id}", headers=HEADERS).json()["data"]
    status = data["status"]
    print(f"status: {status}")
    if status == "success":
        break
    if status == "failed":
        raise RuntimeError(data.get("error"))
    time.sleep(10)   # queued / processing / calibrating → 继续轮询
else:
    raise TimeoutError("task did not finish in time")

# 3. 拉取校对文本：此时内容已就绪，直接 200
text = requests.get(f"{BASE_URL}/view/{view_token}?raw=calibrated").text
print(f"Calibrated ({len(text)} chars):\n{text[:500]}")
```

---

## 其他接口

| 端点 | 方法 | 认证 | 说明 |
|------|------|------|------|
| `/api/recalibrate` | POST | 是（需 `recalibrate` 权限） | 复用已有转录重新跑 LLM 校对，body：`{"view_token": "...", "wechat_webhook": "可选"}`，复用原 view_token |
| `/api/users/profile` | GET | 是 | 当前用户信息（api_key 脱敏返回） |
| `/api/audit/stats` | GET | 是 | 调用统计 |
| `/api/audit/calls` | GET | 是 | 调用记录 |
| `/api/audit/history` | GET | 是 | 任务历史（多条件过滤、分页、关键词） |
| `/api/audit/filter-options` | GET | 是 | 历史过滤选项（webhook/平台/频道列表） |
| `/api/audit/summary` | GET | 是 | 任务摘要预览（前 300 字） |

---

## 补充说明

- **交互式文档**：`https://your-domain.com/docs`（Swagger UI），可在线调试。
- **推送通知**：配置 `wechat_webhook` / `feishu_webhook`（或 `notification_config`）后，任务创建时推送查看链接，全部完成后推送总结 + 「任务完成」消息。校对文本超长（>5000 字）时通知里只放链接，不塞全文。
- **缓存去重**：相同 `平台 + 媒体 ID + 是否说话人识别` 的任务会命中缓存，秒回历史结果。
- **并发限制**：服务端有任务队列，队满返回 HTTP 503，客户端应稍后重试。
