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
| `VIDEO_TRANSCRIPT_API_BASE_URL` | ✅ | 服务地址，不带尾斜杠，如 `http://localhost:8000` 或 `https://vt.example.com` |
| `VIDEO_TRANSCRIPT_API_TOKEN` | ✅ | Bearer token |

脚本运行时会从环境读取。任何一个缺失 → `exit 3` + stderr 明确提示。

## 各平台部署

### Claude Code

1. 把整个 `skill/` 目录放到你的 skill 索引路径下（常见路径：`~/.claude/skills/videotranscript-api/`），或者直接保留在本仓库里用本地路径引用。
2. 在 shell profile（`.zshrc` / `.bashrc`）或项目 `.env` 中：
   ```bash
   export VIDEO_TRANSCRIPT_API_BASE_URL=http://your-server:8000
   export VIDEO_TRANSCRIPT_API_TOKEN=sk-xxx...
   ```

### Hermes

1. 把 `skill/` 同步到 Hermes skill 目录。
2. 运行 `hermes setup` —— 会根据 SKILL.md frontmatter 里的 `metadata.hermes.env` 声明提示你填值。

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
| `submit <url>` | 提交转录任务，异步返回 `task_id` + `view_token` |
| `status <task_id>` | 查任务状态，success 时自动附带 `view_token` |
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
- **异步不阻塞**：`submit` 立返 `task_id`，不在 agent 会话里 poll 十几分钟
- **闭环优先**：`status` 自动反查 view_token，agent 拿 task_id 就能走到 result，不用反问用户
- **清晰的退出码**：让调用方能程序化区分网络问题、业务失败、配置错误

更多设计背景见 [SKILL.md](./SKILL.md)；完整 API schema 见 [references/api.md](./references/api.md)。
