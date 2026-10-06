<!-- delegate-outcome: succeeded -->

# SDK 65 部署进度（card/sdk65-deploy-n305-261004）

- 派发：`dlg-20261004-111840-b18694`
- Issue：#166（本卡不关）
- 固定主干：`ffbddbd10042b8f56f81af711252aa1a257d3a23`（PR #168 MERGED）
- 锁定决策：只换 API 镜像；SDK pin `b0818dc7859d1d8100e42f5c70cb75d34da422f7`；
  server/config/users/env/compose 零更改；凭据不轮换。

## 里程碑

- [x] M0 现场核查（2026-10-04T11:21Z）：worktree 干净，`git log -1` = `ffbddbd1`，`git diff --stat HEAD` 空。
- [x] M0.1 确认未接 D3 自动部署：本仓 `.github/workflows/` 只有 `gate.yml` / `gate-disposition.yml` /
      `gate-shadow.yml`（无 build/push/deploy job）；gate-hub `runner/fleet-manifest.json` 只把本仓列在
      `managed_callers`（gate-v2 调用者），`.github/workflows/gate.yml` 只 `uses:` gate-v2
      （quality/primary/aggregator/notify，无部署）；n305 `crontab -l` 无本项目部署条目；
      生产 compose 用 `image: ${VIDEO_TRANSCRIPT_IMAGE:?...}` 钉 digest，无自动拉取。
- [x] M0.2 升级前生产取证（n305）：
      - 容器 `video-transcript-api`：`Image=ghcr.io/zj1123581321/video-transcript-api@sha256:950a1600da44a341b96886dc47e30585b97c227aebbac6f411307a8353ed0487`，
        `ImageID=sha256:ca46d3a86cebe92684b2cd1ac7d6f1e311ec51890381160c059b08bf46ab8374`，
        `StartedAt=2026-10-03T16:11:07.973881807Z`，`Health=healthy`，`RestartCount=0`。
      - 端口 `8200->8000`，挂载 `config/`、`data/`。
      - `.deploy-image` = 同一 digest `sha256:950a1600…`。
      - 容器内 `GIT_SHA=ff92a175243d`（旧 pin 构建）。
      - 容器内 SDK `client.py` SHA-256 = `ff476ad7cd40401b7ed77c7606cb4fc14dc3d529043c29c4714b199c13423cdf`（**旧 pin，缺陷在生产在线**），
        Python 3.11.17，websockets 15.0.1。
      - CapsWriter 服务端（mac-studio，只读）：PID 22665，`start_server.py`，启动于 Thu Oct 1 18:36:04 2026，
        服务仓 HEAD `6b7a2b82fbc3ebe862250a8804902e5bf37f9211`，`/health`=200。部署前后必须不变。
      - 四文件升级前 SHA-256：
        - `docker-compose.yml` `7b0a89b50de84f71c480c8ef7398d64d479bbaf429805b9401d748294e498188`
        - `config/config.jsonc` `a349b81837e0cb06e2ce3dd8ff670df7e4c107b00e911a8b60670a8a334c25cc`
        - `config/users.json` `237cefb5a2d3bda3ea4e6a227750c348f34475cba1dd0fa804c82458cf28f7ad`
        - `.env` `5885382b22606aafaa4aeb55d529d56739c6555347286183dcae4ddf577b3b1e`
      - 服务器 `docker/pull_and_deploy.sh` SHA-256 = `1d98c3eb9a2f5c6e4e4714b5c54d0915645ff1ae08a834ef7f065eb81d1d5db1`，
        与本 worktree 仓内同名脚本逐字节相同 → **无需同步，无 CRLF 问题**。
- [x] M1 构建并推送：`ghcr.io/zj1123581321/video-transcript-api:ffbddbd10042-sdk65-261004`
      （`--provenance=false --sbom=false --build-arg GIT_SHA=ffbddbd10042b8f56f81af711252aa1a257d3a23`）。
      推前本地镜像内核对 SDK `client.py` = `eccec1a69b81c4a2360d33e15f0ddb8adffb85725dc93d41f8e3150a0863f6c7`。
      registry 独立复核 digest = `sha256:20d683a70c95ef976def9f2c78f1eb31e25bb7fe69634860cfdf9e176d643340`。
- [x] M2 部署（`COMPOSE_FILE_OVERRIDE=/opt/media/VideoTranscriptAPI/docker-compose.yml`，
      未触发 `[MIGRATE]`）：容器 `ImageID=sha256:6116e950…`（= 本地构建产物 config digest）、
      `StartedAt=2026-10-04T11:24:17.07081266Z`、`Health=healthy`、`RestartCount=0`（两次复读未增长）、
      `.deploy-image` = registry 实际 digest、容器内 `GIT_SHA=ffbddbd10042b8f56f81af711252aa1a257d3a23`、
      SDK = `eccec1a6…`（旧 `ff476ad7…` 已不在运行镜像）。四文件 hash 与 M0.2 逐字符相同。
      CapsWriter 服务端 PID 22665 / HEAD `6b7a2b82…` / health 200 全程不变。
- [x] M3 真实 API 验收（仅 1 次，上限 2）：`POST http://127.0.0.1:8200/api/transcribe`
      → HTTP 202，`task_7c71ffc0e4f64b3bbdf47a46b65ada8a`，终态 `success`，
      POST→终态 11.232s，纯 ASR `transcription: 299ms`，管线 total 575ms。
      缓存未命中 → 下载 `asr_example_zh.wav`（0.17MB）→ CapsWriter 调用（尝试 1/5，无重试/无 timeout）→
      1 个 segment、`text_accu=20 字符`；产物 `transcript_capswriter.txt` 60 字节（非空）。
      服务端同任务：源 IP 为 n305 内网地址（已脱敏）、音频 5.55s、19:25:55 连接 → 19:25:56 断开（≈1s，非 120s）。
- [x] M4 样本字节核验：n305 独立下载 `http=200 / 177572 bytes / sha256=a1bd32dc78493c123f9625a66deee562aed2895f53fbc39f2cca3be7e6f4f20f`，
      带本次 `?v=` query 的同一 URL 字节数与哈希完全相同。
- [x] M5 清理：本卡 `/tmp/sdk65-verify`（含 0600 凭据头文件）已删除并复核不存在；
      未删任何既有缓存/数据，未 prune 镜像，未轮换凭据，未改 compose/config。
- [x] M6 报告落盘：`docs/sessions/sdk65-upgrade-261004/deploy/n305-verification.md`。

## 遗留（交主脑裁决，本卡未自行处理）

- **D1**：判据 4 的「服务端同 UUID `task_end(done)`/`result_dispatched`」在生产日志格式下结构性不存在
      （服务端日志近 5000 行这三个关键字计数均为 0；镜像内 SDK `client.py` 只生成 UUID、全文件无相关日志语句）。
      已改用四元组关联（源 IP + 音频 5.55s + 连接窗口 ≈1s + 非空输出）。再发一次任务也不会改变，故未消耗第 2 次额度。
- **D2**：`notification_config.channel="none"` **不是**抑制路由 —— `tasks.py` 只在同时给出 `webhook` 时才赋
      `effective_channel`，本次验收因此产生了一次计划外的企微/飞书运维通知（`task_terminal_notifications.notified_at`
      已被写、attempts=1）。已排除他因（同窗口无其它租户请求、无积压 pending 行）。未改全局配置、未改代码。
- **D3**：卡面指定的 `agent-config/claude/skills/docker-deploy/SKILL.md` 不存在；实际依据
      `agent-config/memory/repos/VideoTranscriptAPI/n305-docker-deploy.md` + `claude/skills/deploy-ops/SKILL.md`。
- **D4**：主脑基线 `gh api request failed`，本卡未跑 CI/测试（阶段 verifying，只部署 + 生产取证）→
      继承红**未能判定**，新红**无**。