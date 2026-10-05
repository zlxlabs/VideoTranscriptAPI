# SDK 492fe19 生产验证原始证据

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
$ curl -s -o /dev/null -w '%{http_code}\\n' https://sum.zlxlabs.com/livez
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

调用行：`transcribe_file_sync(str(path), "ws://127.0.0.1:9")`，未传 `deadline_total`；探针文件为 [verify_sdk71_sdk_probe.py](../../../../scripts/verify_sdk71_sdk_probe.py)。

```text
$ ssh n305 'docker exec -i video-transcript-api /app/.venv/bin/python - c1 /tmp/sdk71-c2-90s.wav' < scripts/verify_sdk71_sdk_probe.py
CALL transcribe_file_sync(path, 'ws://127.0.0.1:9'); deadline_total omitted
RESULT code=connection_lost elapsed_seconds=0.009
exit=0
```

此结果只确认升级后连接拒绝路径未坏。PR #172 既有红绿证据表明旧 pin 上的真实拒绝也会秒级返回；不得将 C1 归因成升级修复。
