<!-- delegate-outcome: succeeded -->

# SDK65 上线独立只读复核（independent-audit）

- Issue：#166（本卡不执行关单，只给 verdict；关单由主脑定局）
- 派发：`dlg-20261004-114054-b9a6f5`（接手 `dlg-20261004-113810-c654aa`，其上家 TLS 断连、无 report 无 commit，非生产缺陷）
- 被审对象：部署执行器报告 `deploy/n305-verification.md`（dispatch `dlg-20261004-111840-b18694`），**本文件不复述其结论，全部结论独立重查**
- 基线 commit：`ffbddbd10042b8f56f81af711252aa1a257d3a23`；本分支 `card/sdk65-prod-audit-261004`
- 资源消耗：SSH **9/12**（n305 ×6，mac-studio ×3）；生产 GET **2/2**（n305 loopback livez、mac /health，各 ≤8s）；**0 个新任务、0 次 POST、0 处生产写入**

## 0. Verdict

**证据链完整，#166 可关。** 五组判据全部独立核实通过，其中判据 3（服务端同 UUID 四事件）取得了比部署报告更强的证据，并证伪其 D1「结构性不存在」论断——事件在 `server_latest.log*` 中精确存在，部署报告只查了 `pm2-out.log`（无 UUID 的粗粒度日志）。通知偏差（D2）两个判定的代码契约只读核实均成立，属请求契约缺口，不阻塞 #166。

## 1. 判据 1：运行镜像 / 资产 / 服务端未变

### 1.1 registry digest（本机只读，独立读 registry，不看 push 输出）

```bash
docker buildx imagetools inspect ghcr.io/zj1123581321/video-transcript-api:ffbddbd10042-sdk65-261004
# Digest: sha256:20d683a70c95ef976def9f2c78f1eb31e25bb7fe69634860cfdf9e176d643340  ✅ 与卡面期望一致
```

### 1.2 容器身份与四文件（SSH n305 #1）

```bash
ssh n305 'docker inspect video-transcript-api --format "Image=... ImageID=... StartedAt=... Health=... Restarts=..."; \
  cat /opt/media/VideoTranscriptAPI/.deploy-image; \
  sha256sum docker-compose.yml config/config.jsonc config/users.json .env'
```

| 项 | 实测 | 期望 | 判定 |
| --- | --- | --- | --- |
| 容器 Image | `…@sha256:20d683a…643340` | registry digest | ✅ 相等 |
| `.deploy-image` | `…@sha256:20d683a…643340` | registry digest | ✅ 相等（独立读 registry 后与容器/.deploy-image 三方一致） |
| ImageID | `sha256:6116e950…80bf89e` | 构建 config digest | ✅ 与 registry manifest config digest 逐位相同 |
| StartedAt | `2026-10-04T11:24:17Z` | 部署后重建 | ✅（旧容器 10-03T16:11Z 已被替换） |
| Health / Restarts | `healthy` / `0` | 未重启 | ✅ |
| compose / config.jsonc / users.json / .env | `7b0a89b5…` / `a349b818…` / `237cefb5…` / `5885382b…` | 卡面升级前四 hash | ✅ 逐字符全等（升级前后字节未变） |

### 1.3 镜像内载荷（SSH n305 #2）

```bash
ssh n305 'docker exec video-transcript-api /app/.venv/bin/python -c \
  "import hashlib,sys,os,capswriter_asr.client as c,websockets; print(...)"'
```

| 项 | 实测 | 期望 | 判定 |
| --- | --- | --- | --- |
| SDK `client.py` SHA-256 | `eccec1a69b81c4a2360d33e15f0ddb8adffb85725dc93d41f8e3150a0863f6c7` | 新 pin `b0818dc` | ✅ |
| Python / websockets | 3.11.17 / 15.0.1 | 3.11（隔离变量只有 SDK） | ✅ |
| `GIT_SHA` | `ffbddbd10042b8f56f81af711252aa1a257d3a23` | 部署固定 SHA | ✅ |

### 1.4 端口 / 挂载 / 健康（SSH n305 #6、mac-studio #3，含 2 次生产 GET）

```bash
ssh n305 'docker inspect video-transcript-api --format "Ports=... Binds=..."; curl -s -m 8 -o /dev/null -w "%{http_code}" http://127.0.0.1:8200/livez'
ssh mac-studio 'curl -s -m 8 -o /dev/null -w "%{http_code}" http://127.0.0.1:6016/health'
```

- 端口映射 `8200:8000`（v4+v6），binds `data/`、`config/`：✅ 未变
- n305 `livez`=200、mac CapsWriter `/health`=200：✅
- 服务端进程未变（SSH mac-studio #2）：listener PID **22665**，启动 `Thu Oct 1 18:36:04 2026`，cwd `capswriter_server_main`，`git rev-parse HEAD` = `6b7a2b82fbc3ebe862250a8804902e5bf37f9211`：与卡面基线完全一致 ✅

## 2. 判据 2：固定任务终态 / 产物 / API 侧定序

### 2.1 SQLite 终态（SSH n305 #3，`mode=ro`，只查固定 task）

```bash
# 经 docker exec -i 跑只读 python：sqlite3.connect("file:/app/data/cache/cache.db?mode=ro", uri=True)
# select task_id,status,completed_at from task_status where task_id='task_7c71ffc0e4f64b3bbdf47a46b65ada8a'
```

`status=success`，`completed_at=2026-10-04 11:25:56`：✅ SQLite 成功终态。`task_terminal_notifications` 该 task 行数=1（存在一条终态通知记录，与 §4 偏差相互印证）。

### 2.2 cache 产物与真实 SDK UUID（SSH n305 #3/#4）

`generic/2026/202610/df618b74f2c1e02b/` 四文件 mtime 均 `2026-10-04T11:25:56Z`（任务新建，非历史缓存）。白名单投影（只读 UUID/计数/长度，不输出正文）：

| 项 | 实测 |
| --- | --- |
| `transcript_capswriter.txt` | 60 字节，非空 ✅ |
| `transcript_capswriter.json` | 375 字节；`task_id=2ab35323-0cd3-4beb-b4e5-677e41ca7fdf`（合法 UUID）；`segments=1`；正文合计 **20 字符**；`duration=5.5466875`；`error=null` |
| `llm_calibrated.txt` / `llm_status.json` | 60B / 1004B |
| workspace `asr_example_zh_funasr.json` | 与 cache JSON **同一 `task_id`**、同尺寸同计数；txt 的 UTF-8 字符数 == 两处 segments 字符数 == 20（60B=20 汉字 ×3B）✅ |

注：FunASR 兼容 JSON 的 schema 是 `{task_id, file_name, duration, segments[], created_at, processing_time, error}`（`capswriter_client.py:687`），**没有**顶层 `text/tokens/timestamps` 键；本复核第一轮按 FunASR 原生键名投影得到全 0，已按正确 schema 复测（上述数字为复测值），不存在产物为空问题。顺带观察（出范围、不影响结论）：`processing_time` 字段为负数（-0.122s），系服务端 `time_complete - time_start` 口径所致，登记为既有行为观察。

### 2.3 API 日志事件定序（SSH n305 #5，独立按行号+时间戳定序）

`docker logs --since 2026-10-04T11:25:50Z --until 2026-10-04T11:26:25Z --timestamps`，窗口内 `timeout|重试|retry|Traceback` 命中数=**0**。关键行（行号为窗口内相对行号）：

| 行 | 时间（UTC） | 事件 |
| --- | --- | --- |
| 24 | 11:25:55.752 | `[缓存检测] 跳过缓存检查 (platform=generic)` |
| 26 | 11:25:55.753 | `[缓存检测] ❌ 缓存未命中，准备下载和转录` |
| 47 | 11:25:56.035 | `文件下载成功 …/asr_example_zh.wav (0.17 MB)` |
| 54 | 11:25:56.072 | `开始转录文件 … (尝试 1/5)` ← CapsWriter 客户端真实被调用 |
| 65 | 11:25:56.352 | `输入数据: text_accu=20 字符, tokens=20, timestamps=20` |
| 66 | 11:25:56.353 | `Segments 生成完成: 1 个片段` |
| 78 | 11:25:56.370 | `[perf] transcription: 299ms (OK)` |
| 107 | 11:25:56.407 | `[perf-summary] total: 575ms` |
| 114 | 11:25:56.408 | `任务状态更新: … -> success` |
| 115 | 11:25:56.416 | `terminal CAS won: … -> success` |
| 148 | 11:26:06.861 | `GET /api/task/task_7c71ffc0… 200 OK` |

定序独立核验通过：未命中旧缓存 → 下载 → CapsWriter 调用 → 转录 **299ms** → success，全程 <1s，远低于 120s 预算；`尝试 1/5` 只出现一次，无重试。299ms 与 20 字符/1 片段与产物侧数字三方一致。

## 3. 判据 3：服务端按产品 UUID 精确事件（核心增量）

UUID 取自 §2.2 cache JSON 白名单字段（**非** API task_id）。服务端日志经 listener 实际 cwd 定位（§1.4），复用 wire 期验证过的安全过滤器（`server_latest.log*` 按 ID 投影，只输出时间/级别/阶段/来源/状态/错误码；先本地语法自检再执行）。SSH mac-studio #4（随机否 ID + 真实 ID + pm2-out 计数一次完成）：

```json
{
  "negative_control": {"task_id": "dfa8ba2a-…", "query_status": "ok", "files_read": 6, "event_count": 0},
  "real_task": {"task_id": "2ab35323-0cd3-4beb-b4e5-677e41ca7fdf", "query_status": "ok", "files_read": 6, "event_count": 4,
    "events": [
      {"time_local": "2026-10-04 19:25:55.914", "level": "INFO",  "phase": "receive_complete",   "source_file": "ws_recv.py", "source_line": 315},
      {"time_local": "2026-10-04 19:25:55.914", "level": "DEBUG", "phase": "final_submit",       "source_file": "ws_recv.py", "source_line": 344},
      {"time_local": "2026-10-04 19:25:56.028", "level": "INFO",  "phase": "task_end", "task_status": "done", "source_file": "state.py", "source_line": 276},
      {"time_local": "2026-10-04 19:25:56.028", "level": "DEBUG", "phase": "result_dispatched",  "source_file": "ws_send.py", "source_line": 204}
    ]},
  "pm2_out_hits_for_real_id": 0
}
```

- **查询成功否 ID 0**：随机否 ID `dfa8ba2a-…` 查询 `query_status=ok`、0 命中 —— 证明「查不到」≠「查询失败」，通道可读（6 个日志文件）。
- **目标结果**：真实 UUID 精确命中 4 事件，`receive_complete → final_submit → task_end(status=done) → result_dispatched`，与 wire 期同 server SHA（`6b7a2b8`）验证过的四事件契约逐位同构。
- **与 pm2-out 的关系**：`pm2-out.log` 对该 UUID 命中 **0**——pm2-out 是不带 UUID 的粗粒度连接日志（连接/断开/时长/模型输出），`server_latest.log*` 才是带 UUID 的结构化日志。部署报告 D1「服务端 UUID 事件在生产日志格式下结构性不存在、再发一次验收任务同样拿不到」**论断不成立**：查错了日志源，不是结构性不可得。四元组关联（IP+时长+窗口+输出）被本 UUID 级关联严格增强，#166 验收口径无需降级。
- 时序自洽：服务端 `task_end(done)` 本地 19:25:56.028（UTC+8 = 11:25:56.028Z）落在 API 侧 `开始转录 11:25:56.072Z` 与 `transcription 299ms 完成 11:25:56.370Z` 之间；服务端 `receive_complete 11:25:55.914Z` 早于 API `开始转录` 日志约 0.16s，判为双机时钟偏移量级，不影响「同一 UUID、同一秒级窗口、done 先于客户端 success」的定性。

## 4. 判据 4：通知偏差只读核验（判定与位置，不改代码不发通知）

代码契约（本地只读，`base` 工作树）：

1. **「channel 无 webhook 被忽略」成立**。`api/routes/tasks.py:287`：`if notification_config and notification_config.webhook:` —— 仅当 webhook 非空才把 `notification_config.channel` 赋给 `effective_channel`。只给 `channel:"none"` 不给 webhook 时，`effective_channel` 保持 `None` → `notification_channel=None` → `router._resolve_targets(None)` 返回**全部已配置渠道**。即：用户以为「none=静默」，实际照常全渠道通知。实测印证：本任务 `task_terminal_notifications` 有 1 行（§2.1），与部署报告 D2 的窗口日志一致。
2. **「none + 非空合法 webhook 已有零路由」成立**。`channel="none"` + 合法 webhook 时：`effective_channel="none"`、`webhooks={"none": url}`（`tasks.py:288-290`）；`NotificationRouter._resolve_targets("none")` 按名过滤，没有任何渠道名为 `none` → `targets=[]`（`utils/notifications/router.py:57-58`）→ 发送循环空转、返回 `{}`；fallback 分支要求 `targets` 非空（`router.py:97/133/170`）→ 不会 fallback 到真实渠道。webhook 先过 `validate_webhook_url` 的 SSRF 校验（`transcription.py:234`），即「合法但零路由」。
3. 性质判定：`notification_config` 无法单独表达「本任务不通知」，属**请求契约缺口/新功能需求**（此前安全实验用 `none + https://example.com` 走的就是零路由这条路，行为未变），不是本次部署引入的回归，也不阻塞 #166。是否登记显式静默开关由主脑/用户裁决，本卡未新增开关、未发任何通知。

## 5. 与部署报告的差异汇总

| 项 | 部署报告 | 本独立复核 |
| --- | --- | --- |
| 服务端 UUID 事件 | D1 判「结构性不存在」，降格四元组关联 | **证伪**：`server_latest.log*` 精确 4 事件；pm2-out 0 命中系日志源差异 |
| 产物内容 | 引用 20 字符/1 片段（API 日志口径） | 同数字从 cache JSON + workspace JSON + txt 字节三方独立复得 |
| 299ms | 引用 perf 日志 | 独立按窗口行号定序复得，且与服务端 done 时序自洽 |
| 通知 | D2 描述现象 | 两个判定落到具体代码行，并补判「none+webhook 零路由」契约 |

其余各项（镜像 digest、SDK hash、GIT_SHA、四文件 hash、端口挂载、服务端未变）与部署报告**一致**，但均为本卡独立重查所得。

## 6. 缺证项与边界

- 无阻塞缺证。固定任务成功终态、产物非空且 UUID 关联、引擎真实调用、服务端 done/dispatched、四文件与镜像身份，五链闭合。
- 本卡未新任务、未 POST、未改任何生产数据/config/代码、未重启服务；服务端零写入。两次生产 GET 均为 loopback 只读健康探测。
- 时钟偏移（n305 vs Mac 约 0.16s 量级）为观察项，不构成缺证。
- 继承红/新红：本卡不跑 CI，基线 `gh api request failed`，按卡面规则**继承红未能判定；新红无**。

## 7. 四问

**踩坑（最隐蔽的一个）**：FunASR 兼容 JSON 没有顶层 `text/tokens/timestamps` 键，第一轮回读全 0——若不复核 schema 直接采信，会把「键名投影错」误报成「产物为空」假缺陷。第二是部署报告的 D1：「在某个日志文件里 grep 不到」被写成了「结构性不存在」；按锁定决策换 `server_latest.log*` 按 UUID 投影，四事件即刻现形——查错日志源不能归因为系统不可得。

**闸**：随机否 ID 先行（`query_status=ok` + 0 命中）区分「无命中」与「查询失败」，过滤器本地 `py_compile` 通过再上生产；远端多行脚本一律单 stdin（`ssh host 'python3 -' < file`）；JSON 只投影 UUID/计数/长度；服务端路径只回 basename；先验值（digest/hash 非空相等）再定结论。

**偏差**：服务端 `receive_complete` 比 API `开始转录` 日志早 0.16s（时钟偏移量级，已观察不定性为缺陷）；cache JSON `processing_time` 为负（既有口径，出范围）。通知契约缺口（§4）为存量行为，非本次引入。

**最贵的一步**：把部署报告 D1 从「结构性不可得」翻案——需要在 listener cwd 下对 6 个 `server_latest.log*` 做 UUID 级投影并用否 ID 校准通道；翻案后 #166 的验收口径从「降级关联」回升到「UUID 级四事件」，这是本卡相对部署报告的净增量。

## 8. 可复核命令清单

本文 §1–§4 各节内联的命令即完整安全命令（含白名单投影脚本，经单 stdin 送达，不在生产落盘）。复核入口：`test -s docs/sessions/sdk65-upgrade-261004/deploy/independent-audit.md`。
