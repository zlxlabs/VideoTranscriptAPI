<!-- delegate-outcome: succeeded -->
<!-- blocked: no -->

# SDK 65 修复 API 镜像上线与真实 CapsWriter 验收（n305）

- Issue：#166（本卡不关、不合并）
- 派发：`dlg-20261004-111840-b18694`
- 固定部署 SHA：`ffbddbd10042b8f56f81af711252aa1a257d3a23`（PR #168 MERGED，主干 `git log -1` 一致）
- 锁定 SDK pin：`b0818dc7859d1d8100e42f5c70cb75d34da422f7`
- 结论：**新镜像已替换生产，真实 POST /api/transcribe 经 CapsWriter 引擎在 299ms 内返回非空产物，任务终态 success。
  服务端（Mac Studio）零改动，config/users/.env/compose 四文件升级前后逐字节相同。**

> 注：判据 4 中「服务端同 UUID 的 `task_end(done)` / `result_dispatched` 事件」在生产日志格式下**结构性不存在**
> （见 §6 偏差 D1），已用可获得的最强四元组关联替代，并说明为何再发一次验收任务也不会改变这一点。

## 1. 上线前基线（2026-10-04T11:21Z 冻结）

| 项 | 值 |
| --- | --- |
| worktree | `sdk65-deploy-n305-261004`，分支 `card/sdk65-deploy-n305-261004` |
| HEAD | `ffbddbd1`（Merge pull request #168），`git diff --stat HEAD` 为空 |
| 生产容器 Image | `ghcr.io/zj1123581321/video-transcript-api@sha256:950a1600da44a341b96886dc47e30585b97c227aebbac6f411307a8353ed0487` |
| 生产容器 ImageID | `sha256:ca46d3a86cebe92684b2cd1ac7d6f1e311ec51890381160c059b08bf46ab8374` |
| 生产 StartedAt | `2026-10-03T16:11:07.973881807Z`，`Health=healthy`，`RestartCount=0` |
| 生产容器 `GIT_SHA` | `ff92a175243d`（**旧 pin 构建**） |
| 生产容器 SDK `client.py` | `ff476ad7cd40401b7ed77c7606cb4fc14dc3d529043c29c4714b199c13423cdf`（**旧 pin，缺陷在线**），Python 3.11.17，websockets 15.0.1 |
| `.deploy-image` | `…@sha256:950a1600…`（与运行镜像一致） |
| CapsWriter 服务端 | PID 22665，`start_server.py`，启动于 Thu Oct 1 18:36:04 2026，仓库 HEAD `6b7a2b82fbc3ebe862250a8804902e5bf37f9211`，`/health`=200 |
| 端口/挂载 | `8200:8000`；`config/`、`data/` |

### 1.1 未接 D3 自动部署（判据 1）

- 本仓 `.github/workflows/` 仅 `gate.yml` / `gate-disposition.yml` / `gate-shadow.yml`，无 build/push/deploy job。
- gate-hub `runner/fleet-manifest.json` 只把 `zlxlabs/VideoTranscriptAPI` 列在 `managed_callers`；
  `gate-hub/.github/workflows/gate.yml` 唯一 `uses:` 是 `zlxlabs/gate/.github/workflows/gate-v2.yml@v2`
  （quality / primary / gate aggregator / notify），无部署 job。
- n305 `crontab -l` 无本项目任何条目（只有备份、镜像 prune、gitea 同步等）。
- 生产 compose 用 `image: ${VIDEO_TRANSCRIPT_IMAGE:?…}` 钉 immutable digest，不存在自动拉取/自动更新路径。

结论：生产镜像只由本次人工部署变更。

### 1.2 四文件升级前 SHA-256（判据 1）

| 文件 | SHA-256 |
| --- | --- |
| `/opt/media/VideoTranscriptAPI/docker-compose.yml` | `7b0a89b50de84f71c480c8ef7398d64d479bbaf429805b9401d748294e498188` |
| `/opt/media/VideoTranscriptAPI/config/config.jsonc` | `a349b81837e0cb06e2ce3dd8ff670df7e4c107b00e911a8b60670a8a334c25cc` |
| `/opt/media/VideoTranscriptAPI/config/users.json` | `237cefb5a2d3bda3ea4e6a227750c348f34475cba1dd0fa804c82458cf28f7ad` |
| `/opt/media/VideoTranscriptAPI/.env` | `5885382b22606aafaa4aeb55d529d56739c6555347286183dcae4ddf577b3b1e` |

部署脚本未同步：服务器 `docker/pull_and_deploy.sh` SHA-256 =
`1d98c3eb9a2f5c6e4e4714b5c54d0915645ff1ae08a834ef7f065eb81d1d5db1`，与本 worktree 仓内同名脚本
**逐字节相同** → 无 CRLF 差异，无需 scp，未触碰服务器 compose。

## 2. 构建与推送（判据 2）

```bash
docker build --provenance=false --sbom=false \
  --build-arg GIT_SHA=ffbddbd10042b8f56f81af711252aa1a257d3a23 \
  -f docker/Dockerfile \
  -t ghcr.io/zj1123581321/video-transcript-api:ffbddbd10042-sdk65-261004 .
```

- 唯一 tag 含 12 位短 SHA + 唯一后缀 `sdk65-261004`，非 `latest`，不覆盖任何已有 tag。
- 推送前先在本地镜像内核对 SDK 载荷（见 §3），确认构建物确实含新 pin 才推。
- 推送输出 digest：`sha256:20d683a70c95ef976def9f2c78f1eb31e25bb7fe69634860cfdf9e176d643340`。
- **registry 独立复核**（不看 push 输出、不看本地副本）：

```bash
docker manifest inspect --verbose ghcr.io/zj1123581321/video-transcript-api:ffbddbd10042-sdk65-261004
# Descriptor.digest = sha256:20d683a70c95ef976def9f2c78f1eb31e25bb7fe69634860cfdf9e176d643340
# mediaType = application/vnd.oci.image.manifest.v1+json（单 manifest，无 attestation 变体）
```

部署即以该 digest 引用（`pull_and_deploy.sh` 内部 `docker pull` 后解析 digest 并用 `VIDEO_TRANSCRIPT_IMAGE=<digest>` 启动）。

## 3. 部署后取证（判据 3）

| 项 | 值 |
| --- | --- |
| `docker inspect` Image | `…@sha256:20d683a70c95ef976def9f2c78f1eb31e25bb7fe69634860cfdf9e176d643340`（= registry 实际 digest） |
| `docker inspect` ImageID | `sha256:6116e9502f5b3787a4d538cd12b8520216a393937314cb6e17f11f39d80bf89e`（= 本地构建产物的 config digest，逐位相同） |
| StartedAt | `2026-10-04T11:24:17.07081266Z`（旧容器为 `2026-10-03T16:11:07Z`，确被重建） |
| Health / Restarts | `healthy` / `RestartCount=0`（部署后两次复读均为 0，未增长）；`Health.Log` 5 条，`ExitCode=0` |
| `.deploy-image` | `…@sha256:20d683a70c95ef976def9f2c78f1eb31e25bb7fe69634860cfdf9e176d643340`（**等于 registry 实际 digest**） |
| 容器内 `GIT_SHA` | `ffbddbd10042b8f56f81af711252aa1a257d3a23` |
| 容器内 SDK `client.py` | `eccec1a69b81c4a2360d33e15f0ddb8adffb85725dc93d41f8e3150a0863f6c7`（新 pin；旧 `ff476ad7…` 已不在运行镜像中） |
| Python / websockets | 3.11.17 / 15.0.1（与旧镜像同版本，隔离变量只有 SDK） |
| 端口/挂载 | `8200:8000`；`config/`、`data/`（未变） |
| CapsWriter 服务端 | PID 22665、启动时间、HEAD `6b7a2b82…`、`/health`=200 —— 部署前后**完全不变** |
| `curl 127.0.0.1:8200/livez` / `/health` | 200 / 200 |

四文件升级后 SHA-256 **与 §1.2 逐字符相同**（部署后即时、验收后各复读一次，共三次读数全等）。

> 健康是必要非充分：本卡的通过依据是 ImageID/StartedAt/SDK 哈希/GIT_SHA 四项同时变更，不是 `Health=healthy`。

## 4. 真实 API 验收（判据 4）

### 4.1 请求契约（白名单 payload）

- 入口：`POST http://127.0.0.1:8200/api/transcribe`（生产宿主 loopback，即生产容器本身；公网入口存在历史 TLS 现象，与本卡无关，未混入）。
- 鉴权：`Authorization: Bearer <token>`，token 由服务器内 `config/users.json` 读出（选第一个 `enabled=true` 的用户，
  共 6 个用户全部 enabled），写入 `0600` 头文件后以 `curl -H @file` 传入 —— **不进 argv、不进 stdout、不进本报告**。
  仅记录：token 长度 67，SHA-256 前 8 位 `5527876e`（用于事后核对是同一枚凭据，不泄露凭据本身）。
- 请求体（全部字段，无敏感值）：

```json
{
  "url": "https://isv-data.oss-cn-hangzhou.aliyuncs.com/ics/MaaS/ASR/test_audio/asr_example_zh.wav?v=sdk65-261004-112555",
  "use_speaker_recognition": false,
  "notification_config": {"channel": "none"},
  "processing_options": {"calibrate": false, "summarize": false, "infer_speaker_names": false, "chapters": false}
}
```

字段选择的理由：
- `use_speaker_recognition=false` → 引擎为 **CapsWriter**（`transcription.py:1066`：`"普通转录(CapsWriter)"`），
  明确排除 FunASR 与字幕路径。
- `processing_options` 全 false → 只转录，**不产生 LLM 耗时**（实测 `llm_processing: 3ms`，未调用任何 LLM），
  因此耗时数字就是纯 ASR 时间，不需要拆分估算。
- URL 加唯一 query `?v=sdk65-261004-112555`：本仓 HTTP API **不存在** `force_refresh`/跳缓存参数
  （`TranscribeRequest` 无该字段，全仓 `force_refresh` 只出现在 FunASR/下载器内部），因此按卡面允许的
  「原公开 URL + 无敏感唯一 query」路径取得全新 `view_token` 与全新缓存键；**没有删除任何既有缓存**。
- `notification_config.channel="none"` 的实际效果见 §6 偏差 D2（未生效）。

### 4.2 缓存与引擎证据（不是缓存命中）

按时间顺序取自容器日志（`docker logs --since 2026-10-04T11:25:50Z --until …T11:26:20Z`，按 `task_id` 与事件关键字白名单投影）：

```
19:25:55 [缓存检测] 跳过缓存检查 (platform=generic, is_generic=True)
19:25:55 [缓存检测] ❌ 缓存未命中，准备下载和转录
19:25:56 文件下载成功: data/temp/task_task_7c71ffc0e4f64b3bbdf47a46b65ada8a/asr_example_zh.wav (大小: 0.17 MB)
19:25:56 [perf] … | download: 248ms (OK)
19:25:56 已配置CapsWriter客户端，服务器: <CapsWriter服务端内网地址>:6016
19:25:56 调用CapsWriter客户端转录文件: …/asr_example_zh.wav
19:25:56 开始转录文件: …/asr_example_zh.wav (尝试 1/5)
19:25:56 transcription_deadline duration=unknown fallback=sdk_auto
19:25:56 输入数据: text_accu=20 字符, tokens=20, timestamps=20
19:25:56 Segments 生成完成: 1 个片段
19:25:56 Segments 统计: 总时长=3.94s
19:25:56 已生成: data/workspace/asr_example_zh.txt / asr_example_zh_funasr.json
19:25:56 [perf] … | transcription: 299ms (OK)
19:25:56 [perf-summary] … | total: 575ms
19:25:56 任务状态更新: … -> success
19:25:56 terminal CAS won: … -> success
```

- 「缓存未命中」在前、下载与 CapsWriter 调用在后 → 引擎确实被调用。
- 新写入的缓存条目 `generic/df618b74f2c1e02b` 目录与全部 4 个文件的 mtime 均为 `2026-10-04T19:25:56`，
  即本次任务新建；随后 4 次 `get_cache: 缓存命中` 发生在 `save_cache` **之后**（日志行号 76-77 写、96-103 读），
  是本次自己写入产物被 LLM 层回读，不是历史缓存冒充。
- 全窗口 `grep -ai "timeout|重试|retry|code=timeout"` **零命中**；`开始转录文件 (尝试 1/5)` 只出现一次，无重试。

### 4.3 服务端同任务证据（Mac Studio，只读）

`capswriter_server_main/logs/pm2-out.log`（行 1743-1751）：

```
2026-10-04T19:25:55: 客户端已连接: <n305内网地址>:<ephemeral-port>
2026-10-04T19:25:55: 音频文件接收完毕，时长 5.55s
2026-10-04T19:25:56:   模型输出：…
2026-10-04T19:25:56:   格式化后：…
2026-10-04T19:25:56: 客户端已断开: <n305内网地址>:<ephemeral-port>
```

四元组关联（同一任务的唯一指纹）：
1. 源 IP 为 n305 宿主的内网地址（私网地址按公开仓出站扫描规则脱敏）；
2. 服务端收得音频时长 **5.55s**，与公开样本 `5.546688s` 及卡面基线一致；
3. 连接→断开窗口 `19:25:55 → 19:25:56`（≈1s），与 API 侧 `transcription: 299ms` 同窗口；
   **旧 pin 的签名行为是客户端挂到 120s 预算耗尽才断开（历史 `task_cc2eeeeb…` 即此形态），本次 1s 内断开。**
4. 服务端返回非空模型输出，API 侧据此生成 1 个 segment、20 字符时间戳序列（`text_accu=20 字符`）。

（服务端返回文本与产物文本一致；按安全约束本报告只记长度，不回显正文。）

### 4.4 终态与产物

| 项 | 值 |
| --- | --- |
| `POST` 返回 | HTTP 202，`code=202`，`data={task_id, view_token}` |
| task_id | `task_7c71ffc0e4f64b3bbdf47a46b65ada8a` |
| 平台 / 引擎 | `platform=generic`，CapsWriter（非 FunASR、非字幕） |
| 终态 | `success`（`GET /api/task/<id>` 与 DB `task_status.status` 一致） |
| POST → 终态 | **11.232s**（11:25:55.683Z → 11:26:06.915Z；含排队与一次轮询间隔） |
| **纯 ASR 耗时** | `transcription: 299ms`；整条管线 `total: 575ms`（远低于 120s 预算） |
| 转录产物 | `data/cache/generic/2026/202610/df618b74f2c1e02b/transcript_capswriter.txt` = **60 字节（非空）**；`transcript_capswriter.json` = 375 字节；`llm_calibrated.txt` = 60 字节 |
| workspace 产物 | `data/workspace/asr_example_zh.txt` = 60 字节；`asr_example_zh_funasr.json` = 375 字节 |
| 公开样本字节 | n305 独立下载核验：`http=200`、`177572` 字节、SHA-256 `a1bd32dc78493c123f9625a66deee562aed2895f53fbc39f2cca3be7e6f4f20f`，与卡面基线逐位相同；**带本次 `?v=` query 的同一 URL 下载结果字节数与哈希完全相同** |
| 临时文件 | 任务结束即被 `tempfile_manager` 清理（`释放 0.17 MB`），符合既有契约 |
| 通知 | 见 §6 偏差 D2（抑制未生效，产生了一次运维侧通知） |

只发了 **1 次**真实验收任务（卡面允许上限 2），一次即取得完整证据。

## 5. 副作用清单与清理

- 副作用共 3 处：① 推一个新 tag 到 GHCR；② 生产容器换镜像（一次 recreate，含一次健康等待）；
  ③ 一个真实转录任务（新建缓存条目 `generic/df618b74f2c1e02b`、一条 `task_status` 行、一条
  `task_terminal_notifications` 行、一次对外通知）。除此之外无任何写操作。
- 本卡自建的临时产物（`/tmp/sdk65-verify`：auth 头文件、请求/响应 JSON、窗口日志、两份样本下载）
  已 `rm -rf` 清理；`ls` 复核目录不存在。
- **未**做任何数据/缓存删除、镜像 prune、凭据轮换、compose/config 改动、CapsWriter 服务端操作、
  DNS/tunnel 操作。既有旧缓存一条未动；生产新生成的缓存条目按契约保留（它是这次真实任务的产物）。
- 未回滚（无需回滚）。旧 immutable 镜像 `sha256:950a1600…` 仍在 n305 本地可用，回滚路径保持可用。

## 6. 偏差与未满足项（如实记录）

**D1｜判据 4 的「服务端同 UUID `task_end(done)` / `result_dispatched`」在生产日志里不存在（未按字面满足）。**
- 事实：服务端 `pm2-out.log` 近 5000 行中 `uuid` / `task_end` / `result_dispatched` / `receive_complete`
  出现次数均为 **0**；服务端日志格式只打印连接/断开/时长/模型输出。
- 事实：镜像内 SDK 源码 `/app/.venv/.../capswriter_asr/client.py` 只在第 338 行 `task_id = str(uuid.uuid4())`
  生成 UUID，**全文件无任何 uuid/task_end/result_dispatched 的日志语句**。
- 因此该事件名只可能来自 triage 期的 wire-probe/代理抓包，不是生产可观测面。再发一次验收任务同样拿不到，
  故未消耗第 2 次额度；改用 §4.3 的四元组关联（源 IP + 音频时长 + 连接窗口 + 非空输出）作为同任务证据。
- 需要主脑裁决：是否把「UUID 级关联」降级为「服务端四元组关联」写入 #166 的验收口径。

**D2｜`notification_config.channel="none"` 不是抑制路由，验收任务产生了一次对外通知（计划外副作用）。**
- 事实：`tasks.py` 中 `effective_channel` 仅在 `notification_config.webhook` 为真时才被赋值；
  只给 `channel` 不给 `webhook` 时，`effective_channel` 保持 `None`，全部渠道照常通知。
  `TranscribeRequest.wechat_webhook` 走空串同样无效（falsy，且被 SSRF 校验）。
- 事实：窗口内 WeCom `Message … sent successfully`、Feishu `message … sent successfully` 多条，
  且 `task_terminal_notifications` 中本任务行 `notified_at='2026-10-04 11:25:56'`、`attempts=1`
  —— 终态通知确实被投递，不是「排入队列但未发送」。
- 归因已排除他因：同窗口 `api_audit_logs` 无其它租户请求；`task_terminal_notifications` 在
  `11:25:00–11:27:00` 区间只有本任务一行；服务启动时的积压投递也无 pending 行（pending 查询为空）。
- 影响面：收信方是运维自己的企微/飞书频道，与该服务每一次生产任务的通知同类同渠道；不含凭据，
  但确实是一条计划外的对外消息（view 链接 + 完成状态 + 校对正文）。
- 未做：没有为了「下次能抑制」去改 `config/config.jsonc` 的全局 webhook（卡面禁止），也没有改代码。
- 建议（交主脑裁决，本卡不动代码）：`notification_config` 目前无法单独表达「本任务不通知」，
  属于请求契约缺口；若需要验收/演练静默，应新增一个显式开关而不是复用 `channel`。

**D3｜卡片指定的必读文件 `agent-config/claude/skills/docker-deploy/SKILL.md` 不存在。**
- 该路径 `ls` 返回 `No such file or directory`；实际承载 n305 部署事实的是
  `agent-config/memory/repos/VideoTranscriptAPI/n305-docker-deploy.md`（已读并按其约束执行：
  不 scp compose、只读服务端、digest 钉版本）。另读 `agent-config/claude/skills/deploy-ops/SKILL.md`。

**D4｜主脑基线不可用（`gh api request failed`）。**
- 本次未跑任何 CI/测试用例（阶段为 verifying，只部署 + 生产取证），因此不存在「继承红 / 新红」判定对象。
  **继承红：未能判定**（无基线可比）；**新红：无**。

## 7. 四问

**踩坑（最隐蔽的一个）**：`notification_config.channel` 在没有 `webhook` 时被静默忽略，
「指定一个不存在的渠道名 = 不通知」这个直觉在代码里不成立 —— 抑制失败发生在 HTTP 层，
而所有下游证据（`notified_at` 被写、企微/飞书 sent successfully）都显示「成功」，不主动查通知侧就发现不了。
第二隐蔽的是：日志里 4 条 `缓存命中` 出现在同一次任务里，但它们全在 `save_cache` **之后**，
只看关键字会误判成「命中缓存冒充引擎成功」；必须用日志行号定序。

**闸（拦住错误动作的机制）**：
- 服务器 compose 用 `image: ${VIDEO_TRANSCRIPT_IMAGE:?…}` 钉 digest，`pull_and_deploy.sh` 在
  `COMPOSE_FILE_OVERRIDE` 存在且渲染不出候选 digest 时直接 `[ERROR]` 退出 → 结构上杜绝了
  「顺手 scp 仓内 compose 覆盖端口 8200」这条路。
- 镜像推送前先跑本地 `docker run` 核对 SDK `client.py` 哈希，不信任「构建成功」等于「构建物正确」。
- 部署判据锚定 ImageID/StartedAt/SDK 哈希/GIT_SHA，不锚定 `Health=healthy`。
- registry digest 用 `docker manifest inspect` 独立读，不拿 `docker push` 输出的 digest 或本地 tag 当结果。

**偏差**：见 §6 的 D1–D4。核心一条（D2）已经发生了一次计划外对外通知，无法在不改全局配置的前提下避免。

**最贵的一步**：核实「这次成功到底是不是缓存冒充 / 引擎有没有真被调用」。
耗时不在命令执行上，而在把三条独立证据链对齐到同一秒级窗口：
容器日志的「缓存未命中 → 下载 → CapsWriter 调用 → transcription 299ms」、
CapsWriter 服务端日志的「5.55s 音频 + 1s 内断开」、以及缓存目录 mtime 全为 `19:25:56`。
如果只信 API 返回的 `success`，旧 pin 的「final 已送达但同步入口 120s 后 timeout、零产物」同样可能
在别处被兜底成 success —— 这次之所以能确证，是因为服务端断开时间与 `transcription` 耗时都远低于 120s 预算。

## 8. 可复核探针契约（主脑独立复跑用）

```bash
# 1) 生产镜像身份（ImageID / StartedAt / 健康与重启）
ssh n305 'docker inspect video-transcript-api --format \
  "Image={{.Config.Image}} ImageID={{.Image}} StartedAt={{.State.StartedAt}} Health={{.State.Health.Status}} Restarts={{.RestartCount}}"'

# 2) .deploy-image 是否等于 registry 实际 digest
ssh n305 'cat /opt/media/VideoTranscriptAPI/.deploy-image'
docker manifest inspect --verbose ghcr.io/zj1123581321/video-transcript-api:ffbddbd10042-sdk65-261004 \
  | grep -m1 '"digest"'

# 3) 镜像内 SDK pin / Python / websockets / GIT_SHA（白名单）
ssh n305 'docker exec video-transcript-api /app/.venv/bin/python -c "
import hashlib,sys,os,capswriter_asr.client as c
print(hashlib.sha256(open(c.__file__,\"rb\").read()).hexdigest()); print(sys.version.split()[0]); print(os.environ[\"GIT_SHA\"])"'

# 4) 四文件未变（与 §1.2 逐字符比对）
ssh n305 'sha256sum /opt/media/VideoTranscriptAPI/docker-compose.yml \
  /opt/media/VideoTranscriptAPI/config/config.jsonc \
  /opt/media/VideoTranscriptAPI/config/users.json /opt/media/VideoTranscriptAPI/.env'

# 5) CapsWriter 服务端未动（PID / 启动时间 / HEAD / 健康）
#    CAPSWRITER_HOME = 服务端 pm2 托管的服务仓根目录（绝对路径属私有基础设施信息，报告内不落盘）
ssh mac-studio 'ps -eo pid,lstart,command | grep start_server.py | grep -v grep'
ssh mac-studio 'cd "$CAPSWRITER_HOME" && git rev-parse HEAD'
ssh mac-studio 'curl -s -m 5 -o /dev/null -w "%{http_code}\n" http://127.0.0.1:6016/health'

# 6) 本次任务的产物与终态（只投影长度与状态，不回显正文/标题）
ssh n305 'docker exec video-transcript-api sh -c \
  "stat -c \"%n %s\" /app/data/cache/generic/2026/202610/df618b74f2c1e02b/*"'
ssh n305 'docker exec -i video-transcript-api /app/.venv/bin/python -c "
import sqlite3;con=sqlite3.connect(\"file:/app/data/cache/cache.db?mode=ro\",uri=True);
print(list(con.execute(\"select task_id,status,completed_at from task_status where task_id=?\",(\"task_7c71ffc0e4f64b3bbdf47a46b65ada8a\",))))"'

# 7) 服务端同任务日志（只读，按时间窗 grep 关键字，不整段回显）
ssh mac-studio 'grep -a "19:25:5" "$CAPSWRITER_HOME/logs/pm2-out.log" | cut -c1-120'
```

安全约束遵守情况：全程未打印 `.env` / `config.jsonc` / `users.json` 内容、未打印 token 值、未回显转录正文/标题、
未输出原始日志大段（均为白名单字段投影 + 行号定序）；每条网络请求单独 `curl` 并显式落私有文件；
SSH 多行脚本一律走 stdin，未拼多层引号；凭据只以长度与哈希前 8 位形式出现。