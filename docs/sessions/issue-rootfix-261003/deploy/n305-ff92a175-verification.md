# n305 生产部署与验收报告 · `ff92a175243d`

- 部署目标：`n305` → `/opt/media/VideoTranscriptAPI`（`docker/deploy_targets.json`）
- 冻结源：`main@ff92a175243dde36143da867a95e2fdad384e16c`（Merge PR #164 / #163 等已验收主干）
- 部署方式：GHCR registry 模式（仓库无 `.github/workflows/deploy.yml`，非 D3 自动发布）
- 执行时间：2026-10-03 16:0x–16:5x UTC（服务器 `docker inspect` / `date` 均为 UTC）
- 执行卡：`dlg-20261003-160300-6b75de`

本报告严格分三段：**已部署新镜像**、**健康验证**、**业务验证（#147/#156/#159）**。任一段缺证据即视为未完成。

---

## 一、已部署新镜像（不是「旧服务还健康」）

### 1.1 发布物与不可变 digest

| 项 | 值 |
| --- | --- |
| registry tag | `ghcr.io/zj1123581321/video-transcript-api:ff92a175243d` |
| registry digest（独立回读） | `sha256:950a1600da44a341b96886dc47e30585b97c227aebbac6f411307a8353ed0487` |
| 本仓发布 tag 长度 | 12 位（`docker/push_to_ghcr.sh` 用 `--short=12`，不是通用示例的 7 位） |

构建/推送前先查过 registry：该 tag **不存在**（`docker manifest inspect … :ff92a175243d` → `manifest unknown`），
所以是全新构建，不存在覆盖不明镜像的情况。

实际执行的 argv（`docker/push_to_ghcr.sh` 的等价实现，额外显式关闭 attestation）：

```
docker build --provenance=false --sbom=false \
  --build-arg GIT_SHA=ff92a175243d \
  -f docker/Dockerfile \
  -t ghcr.io/zj1123581321/video-transcript-api:ff92a175243d .
docker push ghcr.io/zj1123581321/video-transcript-api:ff92a175243d
```

构建在冻结 worktree（`deploy-n305-261003`，`git status --porcelain` 为空）内完成，
本报告文档是在推送+部署成功之后才创建/提交的，不会污染 tag。
push 后再用 `docker buildx imagetools inspect` 从 registry 独立回读 digest，与构建输出一致。

### 1.2 部署命令与不兼容处理

```
ssh n305 'cd /opt/media/VideoTranscriptAPI && \
  COMPOSE_FILE_OVERRIDE=/opt/media/VideoTranscriptAPI/docker-compose.yml \
  bash docker/pull_and_deploy.sh ghcr.io/zj1123581321/video-transcript-api:ff92a175243d'
```

- 显式设置 `COMPOSE_FILE_OVERRIDE` 指向**服务器已有**的 compose，使脚本里「兼容性不匹配就覆盖
  compose」的旧 MIGRATE 分支**不可能触发**——digest 渲染不匹配会直接 fail fast。
- 事实：服务器 compose 的 `image:` 已是 `${VIDEO_TRANSCRIPT_IMAGE:?…}` 形态，脚本第 3 步
  `VIDEO_TRANSCRIPT_IMAGE=<digest> … config` 渲染结果与候选 digest 相等，未进入 MIGRATE 分支
  （部署输出中无 `[MIGRATE]` 行）。
- 部署脚本五步全部走完：`[1/5] pull → [2/5] check-config 预检 → [3/5] up -d → [4/5] health → [5/5] complete`，
  无 `[ERROR]`、无 `[ROLLBACK]`。部署脚本自带回滚未被触发。

### 1.3 before / after 事实对照

| 字段 | before | after |
| --- | --- | --- |
| `Config.Image` | `…@sha256:9b26e310b9b0cef7f560a5d04f3926e5f0e5c170e182ae2bf758fc8dbb598932` | `…@sha256:950a1600da44a341b96886dc47e30585b97c227aebbac6f411307a8353ed0487` |
| `Image`（image_id） | `sha256:e42188abf2b424417911254fc9b7d4f532dd5bbfe0cd377c83e074348f0e8af3` | `sha256:ca46d3a86cebe92684b2cd1ac7d6f1e311ec51890381160c059b08bf46ab8374` |
| `State.StartedAt` | `2026-10-03T10:11:36.984994435Z` | `2026-10-03T16:11:07.973881807Z` |
| `State.Health.Status` | healthy | healthy |
| `RestartCount` | 0 | 0 |
| `.deploy-image` | `…@sha256:9b26e310…` | `…@sha256:950a1600…`（= 本次部署 digest，逐字节相等） |

`after` 的 image_id `sha256:ca46d3a8…` 就是 registry manifest 里 config 段的 digest，
`docker image inspect <image_id>` 的 `RepoDigests` 唯一条目即 `…@sha256:950a1600…`。

### 1.4 新镜像里的代码确实是冻结主干

| 断言 | 值 |
| --- | --- |
| 容器内 `GIT_SHA` | `ff92a175243d`（长度 12） |
| `/app/main.py` sha256 | `06c4088131a267ef76d54b40063a616def9007392e89b44d259fc4db698d2995` |
| `/app/src/video_transcript_api/api/services/transcription.py` sha256 | `72180f32419e79e47c0b48bf0f1abe8a74540e9b182fb03a0adea6b15f4a3b7e` |
| `/app/src/video_transcript_api/llm/segmenters/dialog_segmenter.py` sha256 | `3ce719651dc4c4dbe4cc90afcf2958e48237a2463d73c2d8129362d743266520` |

三个哈希与 `git cat-file blob ff92a175243d:<path> | sha256sum` 的冻结 blob 哈希**逐个相等**
（不是与工作区相等，而是与 git 对象相等）。

### 1.5 未被改动的生产资产（部署前后字节级一致）

| 文件 | sha256（before = after） |
| --- | --- |
| `/opt/media/VideoTranscriptAPI/.env` | `5885382b22606aafaa4aeb55d529d56739c6555347286183dcae4ddf577b3b1e` |
| `config/config.jsonc` | `a349b81837e0cb06e2ce3dd8ff670df7e4c107b00e911a8b60670a8a334c25cc` |
| `config/users.json` | `237cefb5a2d3bda3ea4e6a227750c348f34475cba1dd0fa804c82458cf28f7ad` |
| `docker-compose.yml`（服务器自有） | `7b0a89b50de84f71c480c8ef7398d64d479bbaf429805b9401d748294e498188` |

`.env` 只做了「值长度非空」检查（`SENTRY_DSN` 长度 61、`COMPOSE_PROJECT_NAME` 长度 6），没有 source、
没有打印任何值；`config.jsonc` 用带字符串感知的 jsonc 剥离后只读白名单结构（键名、allowlist 条目），
没有打印 api_key / webhook / token。

### 1.6 服务器 compose 与仓库模板的结构化比对（白名单字段）

| 字段 | 服务器 compose | 仓库 `docker/docker-compose.deploy.yml` |
| --- | --- | --- |
| `image` | `${VIDEO_TRANSCRIPT_IMAGE:?…}` | `${VIDEO_TRANSCRIPT_IMAGE:?…}` |
| `container_name` | `video-transcript-api` | 同 |
| `ports` | `8200:8000` | 同 |
| `volumes` | `./config:/app/config`、`./data:/app/data` | 同 |
| `env_file` | `.env` | 同 |
| `stop_grace_period` | `30s` | 模板未写（服务器侧定制） |
| `mem_limit` / `restart` | `2g` / `unless-stopped` | 同 |
| logging | `json-file`，`max-size=10m`、`max-file=3` | 同 |
| `TZ=Asia/Shanghai` | 有 | 同 |

服务器 compose 是权威，**未 scp 覆盖**；唯一差异 `stop_grace_period: 30s` 是服务器侧定制，按原样保留。
运行中容器实测挂载：`…/data -> /app/data (rw)`、`…/config -> /app/config (rw)`，端口 `8000/tcp => 0.0.0.0:8200`。

---

## 二、健康验证

### 2.1 进程与探针

- `docker inspect`：`status=running`、`Health.Status=healthy`、`RestartCount=0`（见 1.3）。
- 容器内 `GIT_SHA`、三份源文件哈希均在运行中的容器里现算（不是从镜像元数据推断）。
- 部署后有界时刻重复采样（间隔 120 秒，3 次）：

| 采样 | 时刻(UTC) | health | RestartCount | 本机 livez |
| --- | --- | --- | --- | --- |
| 1 | 16:28:22 | healthy | 0 | HTTP 200 |
| 2 | 16:30:2x | healthy | 0 | HTTP 200 |
| 3 | 16:32:2x | healthy | 0 | HTTP 200 |

### 2.2 两个入口分别 curl（各自显式 `-o /dev/null`）

| 入口 | 发起位置 | HTTP |
| --- | --- | --- |
| `http://127.0.0.1:8200/livez` | n305 本机 | **200** |
| `https://sum.lexgogo.site/livez` | 执行器开发机 | **000**（TLS 握手前即失败） |
| `https://sum.lexgogo.site/livez` | n305 本机 | **000**（同上） |

外部入口 000 的归因（**与本次部署无关**，见第六节）：该域名在 1.1.1.1 / 8.8.8.8 / 223.5.5.5
三个解析器上一致解析到 `104.219.250.37` 与 `2.59.170.20`，而 n305 的出口公网 IP 是 `82.153.135.252`，
两者都不是 n305；对两个 IP 分别 `--resolve` 强制握手也都是 `000`，说明失败发生在到达应用之前。
n305 上 `cloudflared` 为 `active`、`NRestarts=0`、日志有 `Registered tunnel connection`。
**判定：外部入口的 DNS/TLS 指向在本卡之前就是坏的，属于既有环境事实，不作为本次部署的回归，也不在本卡范围内修复。**

---

## 三、业务验证（#147 / #156 / #159）

除特别说明外，全部在**新运行镜像的生产容器内**执行（`docker exec -i video-transcript-api uv run python -`），
输入全为合成文本或公开 CC0/测试媒体，未向任何 LLM 发请求，未改生产配置。

一次性脚本共 **37 条断言，failed=0**。

### 3.1 #147 —— 生效 LLM 参数与来源（生产 config，真实 CLI）

命令：`uv run python main.py --check-config --config /app/config/config.jsonc`（容器内实际挂载的生产配置）。

| 断言 | 结果 |
| --- | --- |
| 退出码 0 / 首行含 `Configuration OK` | PASS |
| 末行 JSON 可解析 | PASS |
| 含且仅含 9 个白名单键 | PASS |
| 每项恰为 `{value, source}`，source ∈ {config, default, derived} | PASS |
| **9 项 source 与「按生产配置原始键独立推导」的标签逐项相等** | PASS（mismatch 为空） |
| **9 项 value 与真实解析器 `LLMConfig.from_dict(生产config)` 的字段逐项相等** | PASS（mismatch 为空） |
| 生产 `llm.api_key`（长度 51）不出现在 stdout | PASS |
| stdout 不含企微 webhook 域名 | PASS |

生产实际生效值与来源（这就是「生产跑的是几」的自答）：

```json
{"calibration_concurrent_limit": {"source": "config", "value": 10},
 "enable_threshold": {"source": "config", "value": 3000},
 "max_chunk_length": {"source": "config", "value": 3000},
 "max_segment_size": {"source": "config", "value": 3000},
 "min_chunk_length": {"source": "config", "value": 800},
 "preferred_chunk_length": {"source": "config", "value": 2000},
 "segment_size": {"source": "config", "value": 1500},
 "structured_calibration_for_plain": {"source": "default", "value": true},
 "structured_fallback_strategy": {"source": "derived", "value": "formatted_original"}}
```

8 项来自生产配置显式键，`structured_calibration_for_plain` 是代码缺省，
`structured_fallback_strategy` 走真值派生（生产未显式给 `quality_validation.fallback_strategy`）。
**未修改任何生产取值，也未把生产值对齐示例。**

### 3.2 #156 —— ASCII `!?` 句末切分（真实 `DialogSegmenter`，合成英文长文本）

| 断言 | 结果 |
| --- | --- |
| 短样本 `Is it ok? Yes! Great news! Really? Wow!` 切成 5 片且标点随句 | PASS |
| `''.join(parts) == 原文` | PASS |
| **反向对照**：把 `? !` 去掉后同一句返回 1 片（证明断言对 ASCII 句末敏感） | PASS |
| 长样本（822 字符 > cap 200）被切成 5 片 | PASS |
| 每片长度 ≤ `max_chunk_length`（实测最大 173） | PASS |
| 每片都以 `!`/`?` 结尾，无孤立标点片 | PASS |
| 碎片保留 `id=7` / `speaker_id=A` | PASS |
| 拼接内容与原文一致（忽略空格） | PASS |
| 时间戳单调、首片 `00:00:00`、末片 `00:10:00` | PASS |
| 走完整 `segment()`：5 个 chunk、全部在 cap 内、时间有序且首尾锚定 | PASS |
| 中文样本 `第一句话。第二句话！第三句话？` 仍切 3 片（既有行为不变） | PASS |

### 3.3 #159 —— 音轨准入

**(a) 新镜像内真实 ffmpeg/ffprobe + 真实准入函数**

fixture 由容器内真实 `ffmpeg -f lavfi` 生成（非预置文件、非假 ffprobe）：
`video_only.mp4`（testsrc，ffprobe 实测 `[(video, h264)]`）与
`mixed.mp4`（testsrc + sine，实测 `[(video, h264), (audio, aac)]`）。

| 断言 | 结果 |
| --- | --- |
| 无音轨样本 → 抛 `InvalidMediaError`，消息含具名原因 `no_audio_track` | PASS |
| 消息不含 `media_probe_failed`（不把两种失败混为一谈） | PASS |
| 消息含中文「不含音轨」，用户可归因 | PASS |
| mixed 样本准入放行（返回 None、无异常） | PASS |
| 放行前后文件 sha256 相同（准入不改写媒体字节） | PASS |
| 确实调用了真实 ffprobe（spy 记录到 argv 首元素 `ffprobe`） | PASS |
| 文件不存在 → `media_probe_failed` 且**不含** `no_audio_track` | PASS |

**(b) 真实服务用户入口（`POST /api/transcribe` → 轮询 `/api/task/{id}`）**

- 凭据：Bearer token 在服务器端从 `config/users.json` 读取到变量，长度断言非空，**全程不回显**。
- 通知抑制：按现有 API 契约传 `notification_config={"channel":"none","webhook":"https://example.com/"}`，
  该 channel 名不匹配任何已配置渠道，路由器的目标列表为空且不触发 fallback，**不会打扰生产通知**，
  且没有改动生产任何通知配置。
- 无音轨样本（公开 CC0 视频 `https://filesamples.com/samples/video/mp4/sample_640x360.mp4`，
  本地 ffprobe 复核过只有 1 条 video 流）：

| 断言 | 结果 |
| --- | --- |
| HTTP 200 受理，`task_id=task_f844d8de96e24c0fb6e42086f498f208` | PASS |
| 终态 `failed` | PASS |
| `error` = `该媒体不含音轨，无法转录（no_audio_track）` | PASS |
| 不是笼统的「下载文件失败」 | PASS |
| 该任务 19 条日志里「开始转录文件 / 调用CapsWriter / FunASR / 转录完成」出现次数均为 **0** | PASS（引擎零调用） |
| 准入日志为 `[audio_track_admission] reject …`（真实函数拒绝，不是旁路） | PASS |
| 该任务缓存目录文件列表为空（**没有转录产物、没有缓存伪成功**） | PASS |

- 有音轨样本（公开中文语音样本 wav）走同一条入口：准入日志为
  `[audio_track_admission] admit …`（真实放行），随后进入 ASR 引擎。
- **正常音频主路径成功证据（真实入口，非 helper）**：
  `task_b0556bea197b44bf9208e17dd66e875b`，`POST /api/transcribe`（`use_speaker_recognition=true`，
  FunASR 说话人引擎），公开中文语音样本 wav：

| 断言 | 结果 |
| --- | --- |
| 终态 `success`（`error` 为空） | PASS |
| 真实产物落盘 `data/cache/generic/2026/202610/434d0a179dbe2290/`：`transcript_funasr.json` | PASS |
| 转写内容非空：1 个 segment、20 个字符、1 个 speaker、duration 5.55s、`error` 为空 | PASS |
| 该任务走的仍是同一准入点（先 `admit` 再进引擎） | PASS |

CapsWriter 引擎在同一份 5.5 秒 wav 上**可复现地失败两次**（每次约 120 秒后
`转录文件失败`，见第六节第 2 条）；FunASR 引擎对同一份输入成功。两者是生产同时配置的
两个 ASR 后端，本卡按「真实入口拿到一次真实成功」验收，不改动任何生产配置去迁就某一个引擎。

---

## 四、环境事实与已知阻塞（非本卡引入，未在本卡修复）

1. **外部入口 `https://sum.lexgogo.site` 当前不可用**（HTTP 000，TLS 握手前失败）。DNS 在三大解析器上
   指向 `104.219.250.37` / `2.59.170.20`，都不是 n305 的出口 IP `82.153.135.252`。本机 `:8200/livez` 为 200，
   容器 healthy。判定为既有环境问题，已如实上报，未擅自改 DNS / tunnel 配置。
2. **CapsWriter 引擎对极短音频失败（既有现象，未在本卡定论）**：见第六节第 2 条。
   FunASR 引擎对同一输入已取得真实成功，正常音频主路径的验收缺口已补上。
3. **`.github/workflows/` 无 deploy.yml**，本仓不在 D3 自动发布链路，本次为唯一一次手工发布，
   不存在与流水线 `last_good_tag` 状态机冲突的问题。

## 五、清理与未改动证明

- 临时脚本只落在 n305 的 `/tmp`（`incontainer_check.py`、`api_entry_check.py`、`waitspeech.sh`）与
  执行器本机 `/tmp/vtapi-deploy-ff92a175/`；容器内 fixture 建在 `/tmp/verify159_*` 并在脚本内自删。
- **没有在服务器上开任何常驻服务、没有新增端口监听、没有起临时 HTTP fixture 服务**（外部入口既已不可用，
  且 URL 安全策略只放行生产 allowlist 里的三个内网地址，改配置不在授权范围）。
- 未清旧镜像、未删归档、未删数据、未改 `config/`、`.env`、`users.json`、`docker-compose.yml`（哈希见 1.5）。
- 生产数据里只多了本次 2 条验收任务的记录（1 条 FAILED 无音轨、1 条 FAILED 语音样本），属正常服务行为。

## 六、遗留问题（需要主脑/用户判断）

1. **外部入口域名指向异常**：需要用户确认 `sum.lexgogo.site` 应该由哪台机器/哪条 tunnel 提供，
   当前 DNS 指向的两台都不是 n305。
2. **CapsWriter 引擎对 5.5 秒音频的可复现失败（既有现象，需要另开单定论）**：
   同一份公开中文语音 wav（5.55 秒、16kHz 单声道）走真实入口，`use_speaker_recognition=false`
   时两次都在约 120 秒后 `转录文件失败`（`task_cc2eeeeb…`、`task_c3b6e0be…`，行为完全一致）；
   同一时刻另一条真实用户任务 `task_cfb9a4cc…` 最终 `success`，说明 ASR 服务本身不是全挂。
   仓内 `capswriter_client.py` 的文件模式默认 `file_seg_duration=25` 秒、`file_seg_overlap=2`
   （生产 `capswriter` 段未覆盖这两项，走默认），而本样本远短于一个分段——**这只是假设，未证实**：
   本卡没有改生产配置、没有换更长媒体重试，因此不下结论，只登记现象与两个 task_id 供后续单复现。
   正常音频主路径的成功验收已由 FunASR 引擎的同入口成功任务完成（见 3.3(b)）。
3. **凭据外泄的自省**：核对 `users.json` 结构时用了一次形状 dump，把以 api_key 为 JSON key 的
   token 明文打进了执行器会话日志（未进 git、未进报告、未进任何推送产物）。教训：核对用户表结构
   应只打印键名与长度，不能整对象 dump。

## 七、继承红 / 新红

- 卡面给出的主干基线不可用（`gh api request failed`），因此**继承红未能判定**。
- 本卡未运行仓内测试套件（`make test`），不在授权范围；本卡全部结论均来自生产运行镜像上的实跑证据。