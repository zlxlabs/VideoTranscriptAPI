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

所有 JSON 接口返回统一信封，**没有** `data.status` 字段，任务状态完全由 HTTP 状态码 / `code` 字段表达：

```json
{
  "code": 202,
  "message": "任务已提交",
  "data": { ... }    // 可能为 null
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

> ⚠️ **重要**：任务状态变为「完成」(HTTP 200) 表示 **转录** 已结束，但 **LLM 校对 / 总结是异步后置的**，可能还在跑。
> 因此最可靠的「结果就绪」信号不是 `/api/task`，而是直接轮询内容接口 `?raw=calibrated` 直到返回 **HTTP 200**（详见步骤 3）。
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

状态**仅通过 HTTP 状态码 / `code` 字段判断**（响应体没有 status 字符串）：

| HTTP Code | 含义 | 客户端动作 |
|-----------|------|------------|
| 202 | 排队中 / 处理中 | 继续轮询 |
| 200 | 转录完成 | 转去拉取结果（校对可能仍在生成，见步骤 3） |
| 404 | 任务不存在（task_id 无效或服务重启后丢失） | 停止 |
| 500 | 失败 | 读取 `message` 错误信息 |

**推荐轮询策略**：每 5–10 秒一次，设置最大超时（建议 30 分钟，长音频耗时较久）。

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

`?raw=` 接口的 HTTP 状态码即就绪信号，**下游应轮询它直到 200**：

| HTTP Code | 含义 | 客户端动作 |
|-----------|------|------------|
| 200 | 内容就绪，响应体即正文（顶部含 YAML front matter 元数据） |  取用结果 |
| 202 | 校对/总结仍在生成 | 继续轮询 |
| 404 | 转录已完成但该文件尚未写入（LLM 处理中），或未启用该功能 | 短暂重试 / 放弃 |
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

# 2. 轮询任务状态：靠 HTTP 状态码判断（202=处理中, 200=转录完成, 500=失败）
deadline = time.time() + 30 * 60
while time.time() < deadline:
    r = requests.get(f"{BASE_URL}/api/task/{task_id}", headers=HEADERS)
    if r.status_code == 200:
        print("Transcription done.")
        break
    if r.status_code == 500:
        raise RuntimeError(r.json().get("message"))
    time.sleep(10)

# 3. 拉取校对文本：LLM 校对是后置异步的，轮询 ?raw=calibrated 直到 200
while time.time() < deadline:
    r = requests.get(f"{BASE_URL}/view/{view_token}?raw=calibrated")
    if r.status_code == 200:
        print(f"Calibrated ({len(r.text)} chars):\n{r.text[:500]}")
        break
    if r.status_code in (404, 202):   # 仍在生成 / 文件未就绪
        time.sleep(10)
        continue
    raise RuntimeError(f"Unexpected status: {r.status_code}")
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
