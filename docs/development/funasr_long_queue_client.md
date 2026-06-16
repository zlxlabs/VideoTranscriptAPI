# FunASR 客户端 · 长队列异步轮询适配设计

> 目标：让 `funasr_client.py` 对齐 funASR server 的「异步轮询契约 + 准入控制」更新，在服务端队列较深时保持稳健。
> 范围：**线① 连接解耦** —— 只重写 `funasr_client.py` 内部，3 个 `transcribe_sync` 调用点契约不变（`transcription.py:457 / :885 / :1229`），`max_workers=10` 线程闸保留作为客户端背压。
> Blast radius：1 个文件 + config + 测试。可逆。

> **状态：已实现（2026-06-16）**。代码 `src/video_transcript_api/transcriber/funasr_client.py`，
> 测试 `tests/unit/test_funasr_client_polling.py`（18 用例全绿），config 新增 4 项轮询参数。
> 分片 `queue_full` 的 `finalize_upload`「不重传」优化已落地（见 §5）。

---

## 1. 背景：当前模型 vs 新契约

```
当前 (funasr_client.py)                    新 server 契约（长队列优化后）
┌─────────────────────────┐               ┌──────────────────────────────┐
│ connect → upload(整文件) │               │ upload → 拿 task_id           │
│   ↓                      │               │   ↓ (连接可断, 任务不受影响)  │
│ _wait_for_result()       │               │ task_status_batch 轮询(5~10s) │
│   while True:            │  ◄── 差异 ──► │   first_delay≈estimated_wait_s│
│     recv(timeout=300)    │               │ 终态全集:                     │
│   只认 task_complete/error│               │   completed/failed/           │
│   任何异常→整文件重传重试 │               │   timed_out/cancelled         │
└─────────────────────────┘               │ queue_full→按 retry_after 退避│
                                           │ task_expired→凭 file_hash 重投│
                                           └──────────────────────────────┘
```

## 2. 必修问题（按严重度）

| 级别 | 位置 | 问题 | 影响 |
|------|------|------|------|
| **P0-1** | `funasr_client.py:272` | 直接下标 `response["data"]["estimated_wait_minutes"]`，server 已改名 `estimated_wait_seconds` → **KeyError** | 真实音频多 >5MB 走分片，server 一排队几乎必崩第一次，被外层 catch 整文件重传 |
| **P0-2** | `_upload_*` 的 `else` 分支 | `queue_full`（非致命，带 `retry_after`）落到 else 抛异常 → 固定 5s 重试 + 整文件重传，3 次后判失败 | 长队列正是触发 queue_full 的场景 = 队列一长任务就丢 |
| **P1-1** | `transcribe_with_speaker_recognition` except | 任何连接抖动 → 整文件重传 + 重排队尾 | 长队列连接持有久、断线概率高，重传代价大 |
| **P1-2** | `_wait_for_result` | 只认 `task_complete`/`error`，不认 `timed_out`/`cancelled` | 这些终态下死等到 300s 超时再异常重传 |

## 3. 新内部状态机（线①）

```
transcribe_with_speaker_recognition(audio_path):   # 仍由 transcribe_sync 在 worker 线程调用
  file_hash, file_size = ...
  deadline = now + TOTAL_TIMEOUT
  while now < deadline:
    connect_with_retry()
    try:
      r = _submit(file_hash, …)         # phase1: upload_request → cache命中直接返回
      if r.is_result: return r.result   #          否则上传 → 拿 task_id + first_delay
      return _poll(r.task_id, file_hash, r.first_delay, deadline)   # phase2
    except QueueFull as e: disconnect(); sleep(clamp(e.retry_after)); continue   # 非致命
    except PollMiss:       disconnect(); continue        # task_expired/not_found → 凭hash重投
    except ConnDropped:    disconnect(); continue        # 重连后继续poll, 不重传
    finally: disconnect()
  raise TranscriptionTimeout

_poll(task_id, file_hash, first_delay, deadline):
  sleep(clamp(first_delay, 1, 120))
  while now < deadline:
    send {"type":"task_status_batch","data":{"task_ids":[task_id]}}
    msg = recv(timeout=RECV_TIMEOUT)
    if msg.type != "task_status_batch":      # 期间可能夹杂 task_progress/task_complete push
        handle_push(msg); continue           #   命中自己的 task_complete 也可直接返回
    it = msg.data.items[0]
    match it.status:
      "completed"                       -> return it.result or it.srt_content
      "failed"/"timed_out"/"cancelled"  -> raise FatalTranscription(it.error)
      None  (task_expired/not_found)    -> raise PollMiss
      "pending"/"processing"            -> sleep(POLL_INTERVAL); continue
    # recv ConnectionClosed -> raise ConnDropped (task_id 仍有效, 上层重连续poll)
```

`_submit` 内部要处理的返回分支：`task_complete`（缓存秒回）/ `upload_ready`→上传→(`upload_complete`|`task_queued`|`task_complete`) / `queue_full`（raise QueueFull）。`task_queued` 的 `estimated_wait_seconds` 作为 `first_delay`。

## 4. config 新增（`funasr_spk_server` 块）

```jsonc
"funasr_spk_server": {
  "server_url": "ws://192.168.31.222:8767",
  "max_retries": 3,            // 仅用于 connect/transient
  "retry_delay": 5,            // queue_full 无 retry_after 时的兜底退避
  "connection_timeout": 30,
  "poll_interval": 8,          // 新: 轮询间隔 5~10s
  "poll_recv_timeout": 60,     // 新: 单次轮询响应超时(轮询响应秒级返回, 60s 足够)
  "total_timeout": 3600,       // 新: 单任务总超时(跨所有 phase/retry 的硬上限)
  "first_delay_fallback": 5    // 新: 无 estimated_wait_seconds 时首轮延迟
}
```

## 5. 正确性 / 边界（Code Quality 节）

1. **字段全部 `.get` 防御**：消除 `:272` 这类直接下标。`estimated_wait_seconds` 优先，回退 `estimated_wait_minutes*60`，再回退 `first_delay_fallback`。
2. **typed 异常**：定义 `QueueFull(retry_after)` / `PollMiss` / `ConnDropped` / `FatalTranscription`，让外层 `while` 干净分流，避免 DRY 重复 if/else。
3. **terminal 全集**：`completed/failed/timed_out/cancelled` 全部识别；只认 completed 会让失败任务无限轮询。
4. **poll 期间杂音**：`recv` 到 `type != task_status_batch`（progress / 别的 push）要跳过续轮，不能当成自己的响应解析。
5. **断线不重传**：`websockets.ConnectionClosed` → `ConnDropped` → 重连续 poll；task_id 服务端仍有效。
6. **总超时硬闸**：`deadline` 跨 queue_full 退避 / poll_miss 重投 / 重连 全程生效，杜绝无限循环。
7. **retry_after 钳制**：`clamp(retry_after, 1, 60)`，防服务端给出异常值。
8. **向后兼容**：upload_request 不发 `engine`/`diarize`/`language` → 走 server 默认（diarize=true），行为不变。这些新字段**本次不引入**（不扩范围），仅记为后续可选项。

### 协议核对结论（已查 funASR 协议文档 + server `websocket_handler.py` 源码）
- `task_status_batch` 响应 `items[]` 字段已确认：`{task_id, status, progress, result, srt_content, error}`；
  `completed` 时 JSON 内联 `result`、SRT 内联 `srt_content`；`pending/processing` 三者全 null；
  poll-miss = `status=null` + `error="task_expired"|"task_not_found"`。已据此实现 `_handle_poll_message`。
- 终态全集 `completed/failed/timed_out/cancelled` 已全部识别。

### finalize_upload「不重传」优化（已实现，`_finalize_with_retry`）
`queue_full` 报文：`{type:"queue_full", data:{task_id, retry_after, queue_size, max_queue_size, error, message}}`。
两条路径的 server 行为不同（源自 `websocket_handler.py`）：

```
单文件 queue_full → server 删除已落地文件 → 客户端必须整包重传
                   （走外层 FunASRQueueFull 分支，task_id=None 重投）✓ 不变

分片   queue_full → server 保留 session+已落地文件（按 task_id 存 handler 级，
                   跨连接存活）→ 客户端发 finalize_upload 重试，不重传字节
                   finalize_upload 请求体: {"type":"finalize_upload","data":{"task_id": id}}
                   finalize 响应: upload_complete|task_queued(入队成功) /
                                  queue_full(仍满, 退避再 finalize) /
                                  error(session 丢失 → 凭 file_hash 整体重投)
```

实现位于 `_upload_chunked` 末尾的 `_finalize_with_retry(task_id, response, deadline)`：分片队列满时
按 `retry_after` 退避后只发 `finalize_upload`（同连接保持 session 温），`upload_chunk` 不重发；
仍满则继续 finalize（受 `deadline` 约束）；session 丢失抛 `FunASRPollMiss` 交上层凭 hash 重投。
测试 T13~T15 覆盖：不重传断言（`upload_chunk` 计数=6 非 12）、双 finalize、session 丢失回退。

## 6. 测试计划（tests/unit/，纯英文日志）

用 `FakeWebSocket`（脚本化 recv 队列 + 捕获 send），pytest-asyncio 驱动各路径：

| 用例 | 覆盖 |
|------|------|
| T1 | upload_request 直接 task_complete（缓存命中） |
| T2/T3 | 单文件 / 分片 happy path → poll → completed |
| T4 | queue_full → 按 retry_after 退避 → 重投成功；**断言无整文件重传** |
| T5 | task_queued 带 estimated_wait_seconds → first_delay 被采用并钳制 |
| T6 | poll 终态 failed/timed_out/cancelled → 抛异常，**非无限轮询**（3 子例） |
| T7 | poll_miss（status=null/task_expired）→ 凭 hash 重投成功 |
| T8 | poll 中途断线 → 重连续 poll 成功，**断言 upload 未重发** |
| T9 | total_timeout 超限 → 在界内干净抛出 |
| T10 | poll 期间夹杂 task_progress / 杂 push → 容忍续轮 |
| T11 | **回归**：estimated_wait_minutes 缺失 → 无 KeyError（守 `:272`） |
| T12 | 字段缺失/脏数据 → 全 `.get`，无 KeyError |

## 7. 性能

- poll 8s × 深队列（如 100min）≈ 750~1200 条 status 报文/任务，开销可忽略；task_status_batch 廉价。
- **核心收益**：排队/失败任务不再触发整文件重传（可能数百 MB ×3 次）+ 不再重排队尾。
- `first_delay` 用 ETA 避免入队即空轮询打服务端。
- 连接持有期 keepalive ping 60/120 已设；断线由重连兜底，长队列不再是致命路径。

## 8. 不在本次范围

- 线② 线程解耦（管线在 funASR 处劈开 + 状态机中间态 + task_id 持久化崩溃恢复）—— 收益仅在 >10 任务真并发，与 server 准入限流意图相悖，暂不做。
- `engine`/`diarize`/`language` 请求字段的 opt-in。
- 批量多任务共享一条连接的 task_status_batch（>50 分批）—— 当前每任务一连接，单 id 轮询已够。

> 注：分片 `finalize_upload`「不重传」优化原列为遗留 TODO，现已实现（见 §5）。
