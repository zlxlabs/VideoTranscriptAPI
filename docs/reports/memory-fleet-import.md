# memory-fleet 仓专属事实导入报告

- Task-Id：VideoTranscriptAPI-20260904-01
- Dispatch-Id：dlg-20260903-224723-731fef
- 执行器：cursor / cursor-grok-4.6-high（implementer）
- 分支：`card/VideoTranscriptAPI-20260904-01`
- Base：`5480490a51969d8a807988d410921340055f00d3`
- 日期：2026-09-04
- PR：https://github.com/zlxlabs/VideoTranscriptAPI/pull/69 （#69，未合并）

## 落位清单

| 条目 | kind | 小节标题 | 记录日期 |
|---|---|---|---|
| `reference_n305_docker_deploy` | reference | n305 Docker 部署 | 正文最晚 2026-07-09（frontmatter 无 `modified`） |
| `project_fleet_onboarding` | preference | 运维舰队接入范围 | 2026-07-03（frontmatter 无 `modified`，取自正文拍板日） |
| `project_speaker_attribution` | fact | 说话人归属 | 2026-07-28（frontmatter `modified`） |

文件：`docs/project-memory.md`。仓根 `AGENTS.md` 增加一行指针。

## 脱敏动作清单

- `project_fleet_onboarding`：归档 DSN 原文为 `http://<key>@<ops-host>:9000/29`。把 `<key>` 换成变量名占位 `<SENTRY_DSN_VIDEO_TRANSCRIPT_API>`，写成 `http://<SENTRY_DSN_VIDEO_TRANSCRIPT_API>@<ops-host>:9000/29`。未写入真实 token。`sentry_dsn_secret=SENTRY_DSN_VIDEO_TRANSCRIPT_API` 与 `SENTRY_DSN` 本身是变量名，原样保留。
- `project_speaker_attribution`：无。
- `reference_n305_docker_deploy`：无。正文只写 PAT 权限范围（`write:packages` / `read:packages`）和存储位置 `~/.docker/config.json`，没有 token 值。

## 对「这条以后还有用吗」的异议（只报不删）

- `project_fleet_onboarding`：「两仓 commit 仍在本地 main 未 push」是 2026-07-03 快照，现在多半过期；D5 不接 D3、端口 8200、`/livez` 与 `/health` 分工这些拍板仍有用。
- `reference_n305_docker_deploy`：开源仓写入内网 IP（`192.0.2.13`）和外部入口域名。去向已锁定，本卡不改落点；若日后按 DECISIONS ② 开源仓内网标识收进私有 memory，由主脑另裁。
- `project_speaker_attribution`：无异议。funasr 1.2.7 句级归属机制与 `dlg-{index}` 锚点仍是本仓工作约束。

## 假设调整（相对任务卡默认）

- 小节按主题排：部署指针（n305）→ 舰队接入范围 → 说话人归属，不按原文件名字母序。
- 时间句放在每个小节开头。两条归档没有 `modified`，分别用正文最晚日期 / 拍板日，并在句中写明来源。
- n305 原文的二级标题降成三级，嵌进本仓的二级小节，避免拆成多个平行小节。
- `AGENTS.md` 指针加在文件第二行（中文沟通约定之后、`# Repository Guidelines` 之前）。

## 范围核对

`git diff --name-only` 只应出现：

- `docs/project-memory.md`
- `docs/reports/memory-fleet-import.md`
- `AGENTS.md`

未改 `src/`、测试、`.github/`，也未改 agent-config 仓。
