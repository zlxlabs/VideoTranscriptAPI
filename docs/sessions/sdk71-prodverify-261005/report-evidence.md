# SDK 492fe19 生产验证原始证据

> **脚本已从仓内移除（2026-10-05，pi-lead）**：gate 主审 finding `reliability-api-probe-notification-side-effect` 指出，提交任务的探针脚本无法保证通知静默，运行它必然产生生产通知副作用。这类「唯一作用就是在生产上制造真实副作用」的一次性脚本留在仓里是 footgun，已删除 `scripts/verify_sdk71_api_task.py`；其内容与当次运行记录保留在本分支历史提交 `f1f8116` 及上文原始输出中。仓内只保留不产生生产副作用的 SDK 探针。

> **脱敏声明（2026-10-05，pi-lead）**：本仓对外可见，pre-push public-scan 会拒绝内网地址。
> 下列原始输出中的 ASR 服务端与 live-recorder 内网地址已替换为 `<capswriter-host>` / `<live-recorder-host>` 占位符，其余内容逐字未改。真实端点只存在于不入库的 `config/config.jsonc` 与 `.env`。


## 构建、发布与配置基线

构建源：干净 detached worktree，HEAD `a21d9729b15dac3df2cc0e595dea5426a2d1718f`。

```text
$ BUILDX_NO_DEFAULT_ATTESTATIONS=1 ./docker/push_to_ghcr.sh
[1/2] Building ghcr.io/zj1123581321/video-transcript-api:a21d9729b15d...
[2/2] Pushing ghcr.io/zj1123581321/video-transcript-api:a21d9729b15d...
a21d9729b15d: digest: sha256:e1d6339d9dc3e8c391d1b5f61560a86f43dd02cacaaa90251694cb3ba283486d size: 3158
exit=0

$ docker buildx imagetools inspect --format '{{.Manifest.Digest}}' ghcr.io/zj1123581321/video-transcript-api:a21d9729b15d
sha256:e1d6339d9dc3e8c391d1b5f61560a86f43dd02cacaaa90251694cb3ba283486d
exit=0

$ docker image inspect ghcr.io/zj1123581321/video-transcript-api:a21d9729b15d --format '{{.Config.Env}}' | filter GIT_SHA
GIT_SHA=a21d9729b15d
exit=0
```

部署唯一入口：

```text
$ ssh n305 '/opt/media/VideoTranscriptAPI/docker/pull_and_deploy.sh ghcr.io/zj1123581321/video-transcript-api:a21d9729b15d'
[1/5] Pulling candidate tag...
Digest: sha256:e1d6339d9dc3e8c391d1b5f61560a86f43dd02cacaaa90251694cb3ba283486d
[2/5] Running side-effect-free configuration preflight...
Configuration OK
[3/5] Starting candidate digest...
Container video-transcript-api Recreate
Container video-transcript-api Recreated
Container video-transcript-api Starting
Container video-transcript-api Started
[4/5] Waiting for candidate health...
[5/5] Deployment complete: ghcr.io/zj1123581321/video-transcript-api@sha256:e1d6339d9dc3e8c391d1b5f61560a86f43dd02cacaaa90251694cb3ba283486d
exit=0
```

部署前容器/`.deploy-image` 的回滚点：`ghcr.io/zj1123581321/video-transcript-api@sha256:98681b3c05a82727fe5559e526c220db68047a14d9f55151105406e8ab3fb21a`。
部署后状态原始输出：

```text
2026-10-05 10:45:02 UTC
ghcr.io/zj1123581321/video-transcript-api@sha256:e1d6339d9dc3e8c391d1b5f61560a86f43dd02cacaaa90251694cb3ba283486d
ghcr.io/zj1123581321/video-transcript-api@sha256:e1d6339d9dc3e8c391d1b5f61560a86f43dd02cacaaa90251694cb3ba283486d sha256:61720ff7d2643685fdb2a00254ed2796c51d847b053f61bf72b082c9b4b064da running healthy 2026-10-05T10:44:40.019437247Z
Up 22 seconds (healthy) ghcr.io/zj1123581321/video-transcript-api
sha256:61720ff7d2643685fdb2a00254ed2796c51d847b053f61bf72b082c9b4b064da ghcr.io/zj1123581321/video-transcript-api@sha256:e1d6339d9dc3e8c391d1b5f61560a86f43dd02cacaaa90251694cb3ba283486d
```

生产 API 公网入口（每个 URL 独立执行，显式丢弃响应体）：

```text
$ curl -s -o /dev/null -w '%{http_code}\n' https://sum.zlxlabs.com/livez
200
exit=0
```

配置四文件哈希，部署前：

```text
a349b81837e0cb06e2ce3dd8ff670df7e4c107b00e911a8b60670a8a334c25cc  /opt/media/VideoTranscriptAPI/config/config.jsonc
237cefb5a2d3bda3ea4e6a227750c348f34475cba1dd0fa804c82458cf28f7ad  /opt/media/VideoTranscriptAPI/config/users.json
5885382b22606aafaa4aeb55d529d56739c6555347286183dcae4ddf577b3b1e  /opt/media/VideoTranscriptAPI/.env
7b0a89b50de84f71c480c8ef7398d64d479bbaf429805b9401d748294e498188  /opt/media/VideoTranscriptAPI/docker-compose.yml
```

配置四文件哈希，部署后：

```text
a349b81837e0cb06e2ce3dd8ff670df7e4c107b00e911a8b60670a8a334c25cc  /opt/media/VideoTranscriptAPI/config/config.jsonc
237cefb5a2d3bda3ea4e6a227750c348f34475cba1dd0fa804c82458cf28f7ad  /opt/media/VideoTranscriptAPI/config/users.json
5885382b22606aafaa4aeb55d529d56739c6555347286183dcae4ddf577b3b1e  /opt/media/VideoTranscriptAPI/.env
7b0a89b50de84f71c480c8ef7398d64d479bbaf429805b9401d748294e498188  /opt/media/VideoTranscriptAPI/docker-compose.yml
```

## 真实录制与 C2 切片 producer

C3 来源任务：`250f1810fba9422dbdee6f1a3c236763`，`wechat-channels-live`，`orig_2060970783849858363`。录制文件原始时长 `196.224s`、`3,305,810` bytes，AAC / 48 kHz / 双声道；时长低于 15 分钟。

```text
$ ssh n305 'docker exec live-recorder ffmpeg -v error -ss 20 -i /app/data/recordings/250f1810fba9422dbdee6f1a3c236763/250f1810fba9422dbdee6f1a3c236763.mp4 -t 90 -map 0:a:0 -vn -ac 1 -ar 16000 -c:a pcm_s16le -f wav pipe:1' > /tmp/sdk71-c2-90s.wav
$ ffprobe -v error -show_entries format=duration -show_entries stream=codec_name,sample_rate,channels -of json /tmp/sdk71-c2-90s.wav | jq '{duration: .format.duration, streams: [.streams[] | {codec_name,sample_rate,channels}]}'
{
  "duration": "91.557313",
  "streams": [{"codec_name":"pcm_s16le","sample_rate":"16000","channels":1}]
}
$ sha256sum /tmp/sdk71-c2-90s.wav
36161dd2abc19f565036e23603630214230d772ae59f02000840e62a2f3cfd02  /tmp/sdk71-c2-90s.wav
$ stat -c '%s bytes %n' /tmp/sdk71-c2-90s.wav
2929912 bytes /tmp/sdk71-c2-90s.wav
exit=0

$ scp /tmp/sdk71-c2-90s.wav n305:/tmp/sdk71-c2-90s.wav
exit=0
$ ssh n305 'docker cp /tmp/sdk71-c2-90s.wav video-transcript-api:/tmp/sdk71-c2-90s.wav'
exit=0
$ ssh n305 'docker exec video-transcript-api sh -lc "sha256sum /tmp/sdk71-c2-90s.wav; ffprobe -v error -show_entries format=duration -show_entries stream=codec_name,sample_rate,channels -of json /tmp/sdk71-c2-90s.wav"'
36161dd2abc19f565036e23603630214230d772ae59f02000840e62a2f3cfd02  /tmp/sdk71-c2-90s.wav
exit=0
```

## C1 默认预算路径连接拒绝

调用行：`transcribe_file_sync(str(path), "ws://127.0.0.1:9")`，未传 `deadline_total`；探针文件为 [verify_sdk71_sdk_probe.py](../../../scripts/verify_sdk71_sdk_probe.py)。

```text
$ ssh n305 'docker exec -i video-transcript-api /app/.venv/bin/python - c1 /tmp/sdk71-c2-90s.wav' < scripts/verify_sdk71_sdk_probe.py
CALL transcribe_file_sync(path, 'ws://127.0.0.1:9'); deadline_total omitted
RESULT code=connection_lost elapsed_seconds=0.009
exit=0
```

此结果只确认升级后连接拒绝路径未坏。PR #172 既有红绿证据表明旧 pin 上的真实拒绝也会秒级返回；不得将 C1 归因成升级修复。

## C2 真实 90 秒级音频默认路径

调用行：`transcript = transcribe_file_sync(str(path), url)`，其中 `url="ws://<capswriter-host>:6016"`（ASR 服务端地址取自 `config/config.jsonc`，该文件不入库；此处脱敏，运行时真实值见生产配置）；调用未传 `deadline_total`。音频是上节真实视频号直播录制的 91.557 秒 PCM 切片，SHA256 为 `36161dd2abc19f565036e23603630214230d772ae59f02000840e62a2f3cfd02`，该字节哈希在容器内复核相同。

```text
$ ASR_WS_URL=<取自 config/config.jsonc 的 asr 服务端 ws 地址，前置脱敏故此处不写内网值>
$ ssh n305 "docker exec -i video-transcript-api /app/.venv/bin/python - c2 /tmp/sdk71-c2-90s.wav $ASR_WS_URL" < scripts/verify_sdk71_sdk_probe.py
CALL transcript = transcribe_file_sync(path, 'ws://<capswriter-host>:6016'); deadline_total omitted  # 内网地址已脱敏
RESULT code=done transcript_nonempty=True transcript_chars=395 elapsed_seconds=2.013 sdk_task_uuid=039f1190-a64d-4ebb-b461-cd312e7366a7
exit=0

$ ssh n305 'docker ps --filter name=video-transcript-api --format "{{.Status}} {{.Image}}"; docker exec video-transcript-api sh -lc "find / -name client.py -path \\"*capswriter_asr*\\" -not -path \\"*/git-v1/*\\" | head -1 | xargs sha256sum"; curl -s -o /dev/null -w "%{http_code}\\n" http://127.0.0.1:8200/livez'
Up 4 minutes (healthy) ghcr.io/zj1123581321/video-transcript-api
0490b5f877917e55f995e5ea9a81da226b47cda52e406bf361207c70550eb01b  /root/.cache/uv/archive-v0/GAEenvBIDYHdOc3T/capswriter_asr/client.py
200
exit=0
```

SDK 返回的 UUID 为 `039f1190-a64d-4ebb-b461-cd312e7366a7`，供 C4 服务端事件投影。此样本实际耗时 2.013 秒，远低于旧预算阈值；因此 C2 证明真实音频在新 pin 的默认路径成功、正文非空，但本次输入没有把旧预算推到超时边缘，不将它单独解释为旧 pin 会失败的对照。

## C3 真实 recorder:// 来源经生产 API

来源为既有视频号直播录制 `250f1810fba9422dbdee6f1a3c236763`，全长 `196.224s`（小于 900 秒）；本镜像此前没有该录制的 API 转录任务。请求用生产 API 原生 `recorder://` URL 和录制器文件直链，不退化为容器直调；文件令牌只报告长度，不记录值。

```text
$ ssh n305 'python3 -u - submit' < <探针脚本，见下方「脚本已从仓内移除」说明>
recording_source=recorder://wechat-channels-live/orig_2060970783849858363/250f1810fba9422dbdee6f1a3c236763
recording_duration_seconds=196.224 limit_seconds=900
file_token_length=32
payload.download_url=http://<live-recorder-host>:8080/files/<redacted-file-token>/sdk71-short-recording.mp4  # 内网地址已脱敏
payload_sha256=a9853824c363149ed7f5f216f7b8e01b04643dedb85c1bfb4f555c95a2d9626f
notifications=suppressed via channel sdk71_silent (no registered target)
submitted_at=2026-10-05T10:57:53+00:00 http_status=200 response_code=202 task_id=task_3be6e44a085c4317962a58b631aa9906
exit=1
```

非零是探针断言错误：原脚本把 HTTP 202 当成受理状态，生产 API 实际返回 HTTP 200、应用码 202。按“副作用后非零先读状态”的规则，没有重新 POST，立刻以任务 ID 轮询：

```text
$ ssh n305 'python3 -u - poll task_3be6e44a085c4317962a58b631aa9906' < <同上>
poll_count=1 http_status=200 response_code=202 status=calibrating elapsed_seconds=0.0
poll_count=2 http_status=200 response_code=200 status=success elapsed_seconds=10.0
terminal=success api_code=200 transcript_nonempty=True transcript_chars=791 elapsed_seconds=10.0
exit=0
```

同一请求对应的生产 `app.log` 转录相关行白名单投影（保留时间、来源、事件类别和安全状态/计时字段；不回显 URL 令牌、查看令牌、转录正文）：

```text
task_id=task_3be6e44a085c4317962a58b631aa9906 matching_lines=39
decode_failed_lines=0 samples_total_lines=0 combined_marker_lines=0
known_positive_line=23806 same_pattern_hit=True
line=45880 time=2026-10-05 18:57:53 level=INFO source=video_transcript_api.cache.cache_manager:create_task:2318 event=task_created values=none
line=45881 time=2026-10-05 18:57:53 level=INFO source=video_transcript_api.api.routes.tasks:transcribe_video:320 event=api_queued values=none
line=45883 time=2026-10-05 18:57:53 level=INFO source=video_transcript_api.api.routes.tasks:transcribe_video:379 event=creation_notice_route values=none
line=45884 time=2026-10-05 18:57:53 level=INFO source=video_transcript_api.cache.cache_manager:update_task_status:2470 event=status_update values=status=processing
line=45885 time=2026-10-05 18:57:53 level=INFO source=video_transcript_api.api.services.transcription:process_task_queue:609 event=worker_submitted values=none
line=45887 time=2026-10-05 18:57:53 level=INFO source=video_transcript_api.api.services.transcription:process_transcription:956 event=process_started values=none
line=45891 time=2026-10-05 18:57:53 level=INFO source=video_transcript_api.utils.perf_tracker:track:68 event=performance values=stage=url_parse time=1ms (OK)
line=45893 time=2026-10-05 18:57:53 level=INFO source=video_transcript_api.utils.perf_tracker:track:68 event=performance values=stage=cache_check time=2ms (OK)
line=45905 time=2026-10-05 18:57:55 level=INFO source=video_transcript_api.downloaders.generic:download_file:717 event=download_complete values=none
line=45906 time=2026-10-05 18:57:55 level=INFO source=video_transcript_api.utils.perf_tracker:track:68 event=performance values=stage=download time=1991ms (OK)
line=45907 time=2026-10-05 18:57:55 level=INFO source=video_transcript_api.api.services.transcription:_ensure_audio_track:155 event=audio_admission values=audio_streams=1 streams=1
line=45908 time=2026-10-05 18:57:55 level=INFO source=video_transcript_api.api.services.transcription:process_transcription:2434 event=media_transcription_start values=none
line=45910 time=2026-10-05 18:57:55 level=INFO source=video_transcript_api.transcriber.transcriber:transcribe:107 event=audio_transcription_start values=none
line=45911 time=2026-10-05 18:57:55 level=INFO source=video_transcript_api.transcriber.transcriber:transcribe:123 event=sdk_invoked values=none
line=45912 time=2026-10-05 18:57:55 level=INFO source=video_transcript_api.transcriber.capswriter_client:log:763 event=sdk_file_start values=none
line=45914 time=2026-10-05 18:57:55 level=INFO source=video_transcript_api.transcriber.capswriter_client:log:763 event=sdk_start values=attempt=1/5,duration=None,deadline_total=None
line=45924 time=2026-10-05 18:57:59 level=INFO event=capswriter_done sdk_task_uuid=55802551-6c9e-4aab-b701-a952a3854a8e processed=175.4s events=6 elapsed=4.6s
line=45930 time=2026-10-05 18:58:00 level=INFO source=video_transcript_api.utils.perf_tracker:track:68 event=performance values=stage=transcription time=4625ms (OK)
line=45931 time=2026-10-05 18:58:00 level=INFO source=video_transcript_api.cache.cache_manager:update_task_status:2470 event=status_update values=status=calibrating
line=45932 time=2026-10-05 18:58:00 level=INFO source=video_transcript_api.api.services.transcription:process_transcription:2586 event=llm_queued values=none
line=45933 time=2026-10-05 18:58:00 level=INFO source=video_transcript_api.api.services.llm_ops:process_llm_queue:238 event=other values=none
line=45934 time=2026-10-05 18:58:00 level=INFO source=video_transcript_api.api.services.llm_ops:_handle_llm_task:573 event=llm_started values=none
line=45935 time=2026-10-05 18:58:00 level=INFO source=video_transcript_api.api.services.llm_ops:_handle_llm_task:630 event=llm_coordinator values=none
line=45937 time=2026-10-05 18:58:00 level=INFO source=video_transcript_api.utils.tempfile_manager:clean_up_task:143 event=other values=(释放 3.15 MB)
line=45938 time=2026-10-05 18:58:00 level=INFO source=video_transcript_api.api.services.transcription:run_and_finalize:586 event=other values=none
line=45940 time=2026-10-05 18:58:00 level=INFO source=video_transcript_api.api.services.llm_ops:_handle_llm_task:720 event=other values=none
line=45974 time=2026-10-05 18:58:13 level=INFO source=video_transcript_api.utils.perf_tracker:track:68 event=performance values=stage=llm_processing time=12983ms (OK)
line=45975 time=2026-10-05 18:58:13 level=INFO source=video_transcript_api.api.services.llm_ops:_handle_llm_task:753 event=llm_complete values=none
line=45976 time=2026-10-05 18:58:13 level=INFO source=video_transcript_api.cache.cache_manager:update_task_llm_config:3263 event=llm_saved values=none
line=45977 time=2026-10-05 18:58:13 level=INFO source=video_transcript_api.api.services.llm_ops:_save_llm_results:2016 event=llm_saved values=none
line=45980 time=2026-10-05 18:58:13 level=INFO source=video_transcript_api.api.services.llm_ops:_save_llm_results:2196 event=calibration_saved values=none
line=45981 time=2026-10-05 18:58:13 level=INFO source=video_transcript_api.api.services.llm_ops:_save_llm_results:2230 event=summary_short values=none
line=45987 time=2026-10-05 18:58:13 level=INFO source=video_transcript_api.api.services.llm_ops:_save_llm_results:2380 event=chapter_skipped values=none
line=45993 time=2026-10-05 18:58:13 level=INFO source=video_transcript_api.api.services.llm_ops:_handle_llm_task:824 event=llm_task_complete values=none
line=45994 time=2026-10-05 18:58:13 level=INFO source=video_transcript_api.utils.perf_tracker:log_summary:153 event=perf_summary values=total: 19602ms
line=46000 time=2026-10-05 18:58:13 level=INFO source=video_transcript_api.cache.cache_manager:update_task_status:2470 event=status_update values=status=success
line=46001 time=2026-10-05 18:58:13 level=INFO source=video_transcript_api.api.services.terminal_status:finalize_terminal_status_and_notify:101 event=terminal_cas values=status=success
line=46002 time=2026-10-05 18:58:13 level=INFO source=video_transcript_api.api.services.llm_ops:_handle_llm_task:869 event=task_success values=none
line=46003 time=2026-10-05 18:58:13 level=INFO source=video_transcript_api.api.services.llm_ops:_send_notification:2605 event=other values=none
line=46004 time=2026-10-05 18:58:13 level=INFO source=video_transcript_api.api.services.llm_ops:_send_notification:2645 event=content_notice_route values=none
```

转录器成功终态行处于同一 task 的 SDK start (`45914`) 与 transcription perf (`45930`) 之间；结果为 `capswriter_done`，UUID `55802551-6c9e-4aab-b701-a952a3854a8e`，已处理 `175.4s`，6 个进度事件，4.6 秒。generic 路径仍将媒体时长记为 `None`，本次因 SDK 自动预算完成；这是 #170 观察项，不在本卡修复。`decode_failed` / `samples_total` 判据用同一 regex 先命中已知存在的 line 23806，再对该 task 的 39 行检查，得到 0 条 marker 行。

### C3 通知旁路结果

原输出里的 `notifications=suppressed...` 是错误结论。创建/内容通知选择 `sdk71_silent`，而运行时注册通道名为 `wechat,feishu`；这两处路由无 target。但终态异步 dispatcher 不沿用所选 channel，`task_terminal_notifications` 只读状态为 `success`、`attempts=2`、`notified_at=2026-10-05 10:58:18`。代码约定 `notified_at` 表示已交给异步 notifier，不代表外部送达；因此确认至少有终态通知被提交给配置的 notifier，实际投递回执不可见。未重试 API，也未尝试撤回/清理通知记录。为避免复现误判，受理断言已改为 HTTP 200 + 应用码 202，并将通知提示改成准确说明；此修订没有再次执行 submit。

只读数据库判据原始输出：

```text
db_exists=True row_present=True
status=success created_at=2026-10-05 10:58:13 completed_at=2026-10-05 10:58:13 notified_at=2026-10-05 10:58:18 attempts=2
```

## C4 镜像内 SDK 身份与 CapsWriter 服务端四事件

生产镜像内 SDK 文件 SHA256 在 C2 后核实仍为目标值：

```text
0490b5f877917e55f995e5ea9a81da226b47cda52e406bf361207c70550eb01b  /root/.cache/uv/archive-v0/GAEenvBIDYHdOc3T/capswriter_asr/client.py
```

用上述生产容器 C2 调用得到的 SDK UUID `039f1190-a64d-4ebb-b461-cd312e7366a7` 查 Mac Studio 的 `server_latest.log`。日志格式是纯文本；总计 15 行命中。依据服务端接收路径源码投影四个事件，只打印事件名、行号和白名单字段：

```text
C2_sdk_uuid=039f1190-a64d-4ebb-b461-cd312e7366a7 total_log_lines=15
receive_start_hits=1 lines=42665
receive_complete_hits=1 lines=42682
final_segment_hits=1 lines=42690
final_status_hits=1 lines=42718
receive_duration_values=91.56
final_segment_bytes_values=631124
final_status_values=done
negative_uuid=545188c6-9063-4e5a-89b0-e3c2cc071137 hit_lines=0
```

四事件分别是开始接收音频、音频文件收完、提交最终片段、服务端终态 `done`。否 UUID 是本轮新生成的随机 UUID，在同一文件和同一字面 UUID 搜索规则下 0 命中；正 UUID 15 次命中，说明投影查询有区分力。C4 的四事件日志投影和否对照均已完成。

## 最终生产状态与临时文件清理

清理前先逐份确认三个临时 WAV 的 SHA256 一致，然后只删除本卡创建的精确路径：

```text
$ sha256sum /tmp/sdk71-c2-90s.wav
36161dd2abc19f565036e23603630214230d772ae59f02000840e62a2f3cfd02  /tmp/sdk71-c2-90s.wav
$ ssh n305 'sha256sum /tmp/sdk71-c2-90s.wav'
36161dd2abc19f565036e23603630214230d772ae59f02000840e62a2f3cfd02  /tmp/sdk71-c2-90s.wav
$ ssh n305 'docker exec video-transcript-api sha256sum /tmp/sdk71-c2-90s.wav'
36161dd2abc19f565036e23603630214230d772ae59f02000840e62a2f3cfd02  /tmp/sdk71-c2-90s.wav

$ rm -- /tmp/sdk71-c2-90s.wav
exit=0
$ ssh n305 'rm -- /tmp/sdk71-c2-90s.wav'
exit=0
$ ssh n305 'docker exec video-transcript-api rm -- /tmp/sdk71-c2-90s.wav'
exit=0
$ rm -- /tmp/sdk71-prodverify-evidence-full.md
exit=0
```

用同一路径检查缺失，结果为：

```text
local_media_exists=False
local_projection_copy_exists=False
n305_media_exists=False
container_media_exists=False
production_probe_script_count=0
matching_sdk71_exec_processes=0
```

最终健康与发布指纹：

```text
$ ssh n305 'docker ps --filter name=video-transcript-api --format "{{.Status}} {{.Image}}"'
Up 27 minutes (healthy) ghcr.io/zj1123581321/video-transcript-api
$ ssh n305 'docker inspect -f "{{.State.Status}}/{{.State.Health.Status}}/{{.State.StartedAt}}/{{.Config.Image}}/{{.Image}}" video-transcript-api'
running/healthy/2026-10-05T10:44:40.019437247Z/ghcr.io/zj1123581321/video-transcript-api@sha256:e1d6339d9dc3e8c391d1b5f61560a86f43dd02cacaaa90251694cb3ba283486d/sha256:61720ff7d2643685fdb2a00254ed2796c51d847b053f61bf72b082c9b4b064da
$ ssh n305 'cat /opt/media/VideoTranscriptAPI/.deploy-image'
ghcr.io/zj1123581321/video-transcript-api@sha256:e1d6339d9dc3e8c391d1b5f61560a86f43dd02cacaaa90251694cb3ba283486d
$ curl -s -o /dev/null -w '%{http_code}\\n' https://sum.zlxlabs.com/livez
200
```

容器 SDK 哈希再次读取仍为 `0490b5f877917e55f995e5ea9a81da226b47cda52e406bf361207c70550eb01b`；部署后四项 config/users/.env/compose 哈希也再次读取，与部署前四值完全相同（见本证据开头的前后对照）。容器未回滚，部署目录无本卡探针脚本，临时进程数为 0。
