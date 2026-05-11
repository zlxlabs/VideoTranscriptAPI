---
name: videotranscript-api
description: 通过自部署的 VideoTranscriptAPI 服务，把 YouTube、Bilibili、抖音、小红书、小宇宙播客等平台链接（或直接可访问的音视频直链 URL）转成 AI 校对过的文字稿与总结。当用户分享视频/播客链接并希望拿到文字版、总结、要点、逐字稿时必须使用，即使只是说"这个视频讲了啥"、"帮我听一下这期播客"、"给我这个节目的文字版"、"总结一下这个 B 站视频"、"小宇宙这期在聊什么"也要触发。同样用于查询过去提交过的转录任务历史（按平台、作者、关键词、日期过滤）。注意：服务只接受 URL（平台链接或直链），不支持上传本地文件；若用户想转本地录音，需先把文件放到可访问的 URL 后再提交。
metadata:
  openclaw:
    requires:
      bins:
        - python3
        - python
    env:
      - name: VIDEO_TRANSCRIPT_API_BASE_URL
        description: API 请求地址。通常填内网/tailnet/局域网地址，图的是低延迟。例：http://localhost:8000
        required: true
      - name: VIDEO_TRANSCRIPT_API_TOKEN
        description: Bearer token，对应 config.jsonc 的 api.auth_token 或 users.json 中的 api_key
        required: true
      - name: VIDEO_TRANSCRIPT_API_PUBLIC_URL
        description: 给用户点的公网地址（可选）。不设时用 BASE_URL。例：https://vt.example.com
        required: false
      - name: VIDEO_TRANSCRIPT_API_WECHAT_WEBHOOK
        description: 企业微信 webhook 默认值（可选）。--webhook 传参时覆盖
        required: false
  hermes:
    required_environment_variables:
      - name: VIDEO_TRANSCRIPT_API_BASE_URL
        prompt: API 请求地址（内网优先），如 http://localhost:8000
        help: 服务端监听地址，通常是内网/tailnet/局域网 IP，图的是低延迟
      - name: VIDEO_TRANSCRIPT_API_TOKEN
        prompt: Bearer token
        help: 对应 config.jsonc 的 api.auth_token 或 users.json 中的 api_key
      - name: VIDEO_TRANSCRIPT_API_PUBLIC_URL
        prompt: 给用户点的公网地址（可选，回车跳过）
        help: 不设时用 BASE_URL。服务端在内网但用户从公网看 /view/ 页面时需要
        optional: true
      - name: VIDEO_TRANSCRIPT_API_WECHAT_WEBHOOK
        prompt: 企业微信 webhook（可选，回车跳过）
        help: 设置后 submit 自动推送企微通知，--webhook 传参时覆盖
        optional: true
        required: false
---

# VideoTranscriptAPI Skill

把视频/播客链接丢给自部署的 VideoTranscriptAPI 服务，拿回 AI 校对过的文字稿和 LLM 总结。

## 什么时候用这个 skill

**触发场景**：用户给了一个视频/播客链接（或音视频直链 URL），想要文字化内容或总结。

- 「帮我转录这个视频」「这期播客讲了什么」「给我这个 B 站视频的文字版」
- 「总结一下这个小宇宙节目」「提取这个 YouTube 的要点」
- 「我上次那个转录好了没」（查询历史任务）
- 「我最近转过哪些 YouTube 视频」（按平台过滤历史）

**输入形态**：
- 平台链接 —— YouTube、Bilibili（含 b23.tv 短链）、抖音、小红书、小宇宙播客，自动识别
- 音视频直链 —— `.mp3` / `.mp4` / `.m4a` / `.wav` 等公网可直接 GET 的 URL，走 `submit` 默认参数或 `--download-url`

**不支持的场景**：
- **上传本地文件** —— 服务端没有 multipart 上传端点。若用户拿着一个本地会议录音要转写，需先把文件放到可公网访问的 URL（OSS/S3/Dropbox 直链等）再调 `submit`。
- **视频画面理解** —— 本 skill 只做音轨转录+总结，不分析画面内容。

## 关键约束

**任务是异步的，但 agent 应主动轮询交付结果。** 正确姿势：

1. 调 `submit`，脚本输出中会包含 `view_token` 和一条 `[url](url)` 格式的查看链接
2. **第一步永远是发查看链接**：把脚本输出的查看链接原样复制，单独一条消息发给用户。**无论后续是否立即拿到结果，这一步都不能跳过**——链接是用户日后回看的永久入口
3. 立即调 `result <view_token> --type summary`（可能命中缓存秒返回）
4. 如果返回 202（还在处理），**每隔 1 分钟轮询一次**，最多轮询 10 次（覆盖 0–10 分钟）
5. 返回 200 = 完成，把结果贴给用户；10 次仍未完成则告诉用户「还在处理中，可以点查看链接关注进度」

**查看链接必须从脚本输出中原样复制，严禁自己拼接 URL。** 脚本输出的「查看链接」已经是正确的 `[url](url)` markdown 格式，直接复制发给用户即可。不要凭记忆重写域名或路径——LLM 重构 URL 极易拼错字母（如把 `lexgogo` 写成 `lexgugo`），导致链接失效。

## 环境变量

skill 在调用时从环境读取，**不要**在对话里要求用户粘贴 token。

| 变量 | 必填 | 用途 | 示例 |
|------|-----|------|------|
| `VIDEO_TRANSCRIPT_API_BASE_URL` | ✅ | 脚本发 API 请求用的地址 | `http://localhost:8000` / `http://100.68.21.80:8200`（tailnet）|
| `VIDEO_TRANSCRIPT_API_TOKEN` | ✅ | Bearer token | `config.jsonc` 里 `api.auth_token` 或 `users.json` 的某个 key |
| `VIDEO_TRANSCRIPT_API_PUBLIC_URL` | —  | 给用户点的**公网**地址，不设时用 BASE_URL | `https://vt.example.com` |
| `VIDEO_TRANSCRIPT_API_WECHAT_WEBHOOK` | —  | 企业微信 webhook 默认值，`--webhook` 传参时覆盖 | `https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxx` |

**为啥分两个地址**：服务端常跑在内网/tailnet（BASE_URL 用这个，请求最快），但用户拿 `/view/<token>` 链接可能从公网打开。配 PUBLIC_URL 后，skill 返回给用户的查看页面 URL 用公网域名拼，无论用户在哪张网都点得开。没配 PUBLIC_URL 的场景（纯本机 / 都在一张网内）行为不变。

各平台配置方式：
- **Claude Code**：写入 `~/.claude/settings.json` 的 `"env"` 字段（推荐，跨平台通用）；也可以写 shell profile 的 `export`，但 Windows 上 Git Bash 不一定会 source
- **Hermes**：写入 `~/.hermes/.env`（`KEY=VALUE` 格式），或跑 `hermes setup` 交互式填入
- **OpenClaw**：编辑 `~/.openclaw/openclaw.json`，在 `skills.entries.videotranscript-api.env` 下加这两三个

缺失必填变量时脚本会用 exit 3 和明确的 stderr 消息报错。

## 命令总览

所有命令通过 `python3 <skill>/scripts/videotranscript.py <sub> ...` 调用，输出默认是 markdown，加 `--format json` 拿结构化结果。

**Windows 注意**：Windows 上 `python3` 是系统占位程序（只会打开 Microsoft Store），实际的 Python 命令是 `python`。如果 `python3` 执行失败（exit code 非 0/1/2/3），改用 `python` 重试。

### submit — 提交转录任务

```bash
python3 scripts/videotranscript.py submit <url> [options]
```

常用 options：
- `--speaker`：启用说话人识别（FunASR 引擎，结果会按说话人分段，但更慢）
- `--webhook URL`：完成后推送企业微信（覆盖服务端默认 webhook）
- `--title "..."`, `--author "..."`, `--description "..."`：覆盖自动解析的元数据
- `--download-url URL`：绕过平台解析，用直链下载音视频

返回 `view_token`（和 task_id，可忽略）。**只需要记住 `view_token`**，后续查进度和拉结果都用它。

#### 什么时候该加 `--speaker`

默认**不加**，走 CapsWriter 通用转录，更快。只有"谁说了什么"这件事对用户重要时才加。

| 内容类型 | 是否加 `--speaker` | 理由 |
|---------|------------------|------|
| 多人访谈/对谈播客（如多人小宇宙节目、圆桌讨论） | ✅ 加 | 分段后才看得出谁的观点 |
| 会议录音、座谈、辩论 | ✅ 加 | 同上 |
| 用户明确说"分段"、"谁说的"、"区分讲话人"、"问答格式" | ✅ 加 | 用户意图明确 |
| 单人讲解、教程、vlog、单人脱口秀 | ❌ 不加 | 只有一个人，分段没意义 |
| 单 UP 主口播视频、课程录播、单人解读 | ❌ 不加 | 同上 |
| 用户只要"总结"或"要点" | ❌ 不加 | `summary` 不需要分段，省时间 |
| 拿不准（比如不知道是单人还是多人） | ❌ 不加 | 更快；如果结果发现是多人再让用户重新提交 |

判断时可以参考视频标题、平台、作者名。比如小宇宙"XX 对话 YY"、YouTube 的 `interview with`、标题含"对谈/圆桌/访谈"等就倾向加；一人名+"讲"、"教程"、"实录"、"vlog"倾向不加。

### result — 查进度 & 拉结果

```bash
python3 scripts/videotranscript.py result <view_token> --type <summary|calibrated|transcript>
```

- `summary`（默认）：LLM 生成的要点总结 —— **绝大多数场景就用这个**
- `calibrated`：LLM 校对过的完整文字稿（去除 ASR 错别字、加标点、分段）
- `transcript`：原始 ASR 输出（FunASR JSON 或 CapsWriter 纯文本，一般不用）

任务还没完成时会返回 exit 0 + 一行提示"仍在处理中"；拿不到结果时返回 exit 1。

### history — 查历史任务

```bash
python3 scripts/videotranscript.py history [options]
```

Options：`--platform youtube`、`--author 作者名`、`--q 关键词`、`--start 2026-01-01 --end 2026-01-31`、`--status success`、`--limit 20 --offset 0`。

### 辅助命令

- `status <task_id>`：用 task_id 查详细进度（一般不需要，`result` 已经能判断是否完成）
- `filter-options`：列出服务端已有的平台/作者/webhook 列表（做下拉时用）
- `profile`：查当前 token 对应的用户信息
- `health`：无鉴权探活，用来排查连不上的问题

## 典型对话流

**场景 A：用户想要一个 B 站视频的总结**

1. 用户：「帮我看看这个视频 https://www.bilibili.com/video/BVxxx 讲了什么」
2. 调 `submit https://www.bilibili.com/video/BVxxx`，脚本输出查看链接和 `view_token`
3. **把脚本输出的查看链接原样复制，单独发一条消息给用户**
4. 立即调 `result <view_token> --type summary`，如果命中缓存直接拿到结果
5. 返回 202 → 每隔 1 分钟重试，直到拿到结果（最多 10 次）
6. 拿到结果后把总结贴给用户

**场景 B：查找最近转过的小宇宙节目**

1. 用户：「我上周转过的那个小宇宙播客在哪」
2. 调 `history --platform xiaoyuzhou --start 2026-04-16 --end 2026-04-23`
3. 把结果列表贴给用户，附上 `view_token` 和查看页面链接

**场景 C：想要带说话人分段的多人对话**

1. 用户：「这个访谈有三个人，能帮我做成问答格式吗」
2. 调 `submit <url> --speaker`
3. 其他流程同场景 A；`calibrated` 结果里会带说话人标记

## 退出码语义（程序化调用时看这个）

| Exit | 含义 |
|------|------|
| 0 | 成功 |
| 1 | 业务失败（任务 failed、view_token 无效、结果为空） |
| 2 | 传输/基础设施问题（网络、5xx、401/403 鉴权失败） |
| 3 | 配置问题（缺 env、参数非法） |

遇到 exit 2 时别无脑重试 —— 先看 stderr，通常是 token 错或服务没起。遇到 503（队列满）可以隔 1–2 分钟退避重试。

## Python 脚本跑不起来时的 HTTP Fallback

如果 `python3` 不可用或脚本执行报错（比如 Windows 环境兼容问题），**直接用 curl 调 HTTP 端点**。关键端点映射：

```bash
# 提交任务（需要 token）
curl -X POST "$BASE_URL/api/transcribe" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"url":"https://..."}'

# 查任务状态（需要 token）
curl -s -H "Authorization: Bearer $TOKEN" "$BASE_URL/api/task/$TASK_ID"

# 拉总结（不需要 token）
curl -s "$BASE_URL/view/$VIEW_TOKEN?raw=summary"

# 拉校对后全文（不需要 token）
curl -s "$BASE_URL/view/$VIEW_TOKEN?raw=calibrated"

# 拉原始转录（不需要 token）
curl -s "$BASE_URL/view/$VIEW_TOKEN?raw=transcript"

# 历史查询（需要 token）
curl -s -H "Authorization: Bearer $TOKEN" "$BASE_URL/api/audit/history?platform=youtube&limit=20"
```

**注意端点不要搞混**：
- 拉结果文本用 `/view/{view_token}?raw=<type>`，**不是** `/api/result/...`（不存在这个端点）
- `/view/` 路径**不需要鉴权**，`/api/` 路径需要

## 更深入的 API 细节

完整端点/字段/错误码对照参见 `references/api.md`。只有需要非标准字段、调试异常响应或自定义调用时才去读它。
