# videotranscript-api skill

把 [VideoTranscriptAPI](../README.md) 封装为一个 **agentskills.io 标准 skill**，让下游 agent（Claude Code / OpenClaw / Hermes 等）能直接调用它做视频/播客转录、总结、历史检索。

这份 README 面向**部署 skill 的人**。skill 本身的使用逻辑在 [`SKILL.md`](./SKILL.md) 里，由 agent 自己读。

---

## 目录结构

```
skill/
├── SKILL.md                     # skill 元数据 + 行为指引（给 agent 读）
├── scripts/
│   └── videotranscript.py       # stdlib-only Python CLI，无外部依赖
├── references/
│   └── api.md                   # 完整 API 端点参考（按需加载）
├── evals/
│   └── evals.json               # 5 条回归测试 prompt
└── README.md                    # 本文件
```

## 先决条件

- **Python 3.9+**（脚本只用 stdlib，不需要 pip install 任何东西）
- **一个可访问的 VideoTranscriptAPI 实例**（本仓库根目录的服务，跑起来后监听 `:8000`，或走你的自部署地址）
- **Bearer Token**：`config/config.jsonc` 的 `api.auth_token`，或多用户模式下 `config/users.json` 里某个 key

## 环境变量

| 变量 | 必填 | 说明 |
|------|------|------|
| `VIDEO_TRANSCRIPT_API_BASE_URL` | ✅ | **API 请求地址**。内网/tailnet/局域网优先，延迟低。如 `http://localhost:8000` / `http://100.68.21.80:8200` |
| `VIDEO_TRANSCRIPT_API_TOKEN` | ✅ | Bearer token（`config.jsonc` 的 `api.auth_token` 或 `users.json` 里的 key）|
| `VIDEO_TRANSCRIPT_API_PUBLIC_URL` | — | **给用户点的公网地址**（可选）。不设时用 BASE_URL。如 `https://vt.example.com` |
| `VIDEO_TRANSCRIPT_API_WECHAT_WEBHOOK` | — | **企业微信 webhook 默认值**（可选）。设置后 submit 自动带上，`--webhook` 传参时覆盖。如 `https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxx` |

**为什么分两个地址**：API 请求追求低延迟，通常走内网；但返给用户的 `/view/<token>` 链接需要能从公网打开。配了 PUBLIC_URL 后，`submit` / `status` / `history` 打印给用户的链接会自动用公网域名拼。

脚本运行时会从环境读取。必填变量缺失 → `exit 3` + stderr 明确提示。

## 各平台部署

### Claude Code

1. 把整个 `skill/` 目录放到你的 skill 索引路径下（常见路径：`~/.claude/skills/videotranscript-api/`），或者直接保留在本仓库里用本地路径引用。
2. 配置环境变量（推荐方式：写入 `~/.claude/settings.json` 的 `env` 字段，跨平台通用）：
   ```json
   {
     "env": {
       "VIDEO_TRANSCRIPT_API_BASE_URL": "http://your-server:8000",
       "VIDEO_TRANSCRIPT_API_TOKEN": "sk-xxx...",
       "VIDEO_TRANSCRIPT_API_PUBLIC_URL": "https://vt.example.com",
       "VIDEO_TRANSCRIPT_API_WECHAT_WEBHOOK": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxx"
     }
   }
   ```
   也可以用传统方式写入 shell profile（`.zshrc` / `.bashrc`）的 `export` 语句，但 Windows 上不推荐（Git Bash 不一定会 source）。

**首次安装 prompt**（复制后把 `<...>` 占位符换成实际值）：

```
帮我安装 videotranscript-api skill。

1. 把 https://github.com/zj1123581321/VideoTranscriptAPI.git 仓库里的 skill/ 目录复制到 ~/.claude/skills/videotranscript-api/
2. 在 ~/.claude/settings.json 中配置环境变量（跨平台通用，不依赖 shell）：
   在 JSON 顶层加一个 "env" 字段（如果已有则合并进去）：
   {
     "env": {
       "VIDEO_TRANSCRIPT_API_BASE_URL": "<API 服务地址>",
       "VIDEO_TRANSCRIPT_API_TOKEN": "<Bearer token>",
       "VIDEO_TRANSCRIPT_API_PUBLIC_URL": "<公网地址，可选，不设时用 BASE_URL>",
       "VIDEO_TRANSCRIPT_API_WECHAT_WEBHOOK": "<企微 webhook，可选>"
     }
   }
   注意：不要覆盖 settings.json 里已有的其他配置，只合并 env 部分。
3. 验证连通性：
   - 先试 python3（macOS/Linux）或 python（Windows）运行：
     python3 ~/.claude/skills/videotranscript-api/scripts/videotranscript.py health
   - 如果 python 不可用，用 curl 验证：
     curl -s <API 服务地址>/health

注意：Windows 上 python3 命令是微软商店占位程序，会直接失败。请用 python 代替。
```

**更新 prompt**（skill 已安装，只需拉最新代码）：

```
更新 videotranscript-api skill：从 https://github.com/zj1123581321/VideoTranscriptAPI.git 拉取最新的 skill/ 目录，覆盖到 ~/.claude/skills/videotranscript-api/。环境变量不用动。更新后跑 health 验证。
```

### Hermes

1. 把 `skill/` 同步到 Hermes skill 目录（`~/.hermes/skills/videotranscript-api/`）。
2. 环境变量写入 `~/.hermes/.env`（Hermes 的标准持久化方式，每次 skill 执行时自动注入）。也可以跑 `hermes setup`，会根据 SKILL.md frontmatter 里的 `required_environment_variables` 交互式提示填值。

**首次安装 prompt**（复制后把 `<...>` 占位符换成实际值）：

```
帮我安装 videotranscript-api skill。

1. 把 https://github.com/zj1123581321/VideoTranscriptAPI.git 仓库里的 skill/ 目录复制到 ~/.hermes/skills/videotranscript-api/
2. 在 ~/.hermes/.env 文件中追加以下环境变量（如果文件不存在就创建，注意不要覆盖已有内容）：
   VIDEO_TRANSCRIPT_API_BASE_URL=<API 服务地址，内网优先>
   VIDEO_TRANSCRIPT_API_TOKEN=<Bearer token>
   VIDEO_TRANSCRIPT_API_PUBLIC_URL=<公网地址，可选，不设时用 BASE_URL>
   VIDEO_TRANSCRIPT_API_WECHAT_WEBHOOK=<企微 webhook，可选>
3. 验证连通性：先试 python3 跑 health 命令，python3 不可用则用 python，都不行就用 curl <API 服务地址>/health
```

**更新 prompt**（skill 已安装，只需拉最新代码）：

```
更新 videotranscript-api skill：从 https://github.com/zj1123581321/VideoTranscriptAPI.git 拉取最新的 skill/ 目录，覆盖到 Hermes 的 skill 目录下的 videotranscript-api/。环境变量不用动。更新后跑 health 验证。
```

### OpenClaw

1. 把 `skill/` 放到 OpenClaw 读取的 skill 目录。
2. 编辑 `~/.openclaw/openclaw.json`，在 `skills.entries.videotranscript-api.env` 下加入两个变量。如果你的 agent 跑在 sandbox 里，同时在 `agents.defaults.sandbox.docker.env` 里重复一次。

## 烟测

配好环境变量后：

```bash
# 探活（无需 token 就能跑，但会读 BASE_URL）
python3 skill/scripts/videotranscript.py health

# 查当前 token 对应的用户
python3 skill/scripts/videotranscript.py profile

# 提交一个测试任务（幂等：同一 URL 会命中缓存秒返回）
python3 skill/scripts/videotranscript.py submit https://b23.tv/DBUt7OW
```

退出码语义：

| Exit | 含义 |
|------|------|
| 0 | 成功 |
| 1 | 业务失败（任务 failed、view_token 无效、结果为空） |
| 2 | 传输/基础设施（网络、5xx、401/403 鉴权失败） |
| 3 | 配置（缺 env、参数非法） |

## CLI 子命令速查

| 子命令 | 作用 |
|--------|------|
| `submit <url>` | 提交转录任务，返回查看链接（agent 内部记录 task_id 用于后续查询） |
| `status <task_id>` | 查任务状态（agent 内部使用），success 时自动附带 view_token |
| `result <view_token>` | 拉结果文本（`--type summary`/`calibrated`/`transcript`） |
| `history` | 按平台/作者/关键词/日期/状态查历史任务 |
| `filter-options` | 列出可选的平台/作者/webhook |
| `profile` | 当前用户信息 |
| `health` | 服务健康探活 |

加 `--format json` 任意命令都能给出结构化 JSON；默认 markdown 适合直接喂给 LLM。

## 更新与测试

修改 `scripts/videotranscript.py` 或 `SKILL.md` 后，用 `evals/evals.json` 里的 5 条 prompt 回归：

```bash
# 跟真实服务端对打的烟测
export VIDEO_TRANSCRIPT_API_BASE_URL=...
export VIDEO_TRANSCRIPT_API_TOKEN=...
python3 skill/scripts/videotranscript.py health && echo OK
```

完整 agent 行为测试（带/不带 skill 对比）见 skill-creator 框架：跑完会在 `skill-workspace/iteration-N/` 输出每个 case 的 `response.md` 和 `benchmark.md`。

## 设计要点

- **stdlib-only**：脚本零依赖，`python3` 有就能跑
- **env 驱动**：不在 skill 里硬编码地址或 token，每个下游平台按自己的规范注入
- **异步不阻塞**：`submit` 立返查看链接，不在 agent 会话里 poll 十几分钟
- **闭环优先**：`status` 自动反查 view_token，agent 内部用 task_id 闭环到 result，不用反问用户
- **用户友好**：用户只看到查看链接，task_id 等内部标识不暴露
- **清晰的退出码**：让调用方能程序化区分网络问题、业务失败、配置错误

更多设计背景见 [SKILL.md](./SKILL.md)；完整 API schema 见 [references/api.md](./references/api.md)。
