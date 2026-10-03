<!-- delegate-outcome: succeeded -->

## 结论

failure-visibility: p1-found

生产修复已实测运行在 ff92 镜像，且下列 issue 的原范围证据齐全；建议 #147、#156、#159 可按各自原范围关闭。本次未关闭或修改 issue。

另有一个独立的公网入口故障现象：开发机与 n305 请求预期入口时均在 TLS 握手失败（curl exit 35，HTTP 000），而 n305 本机 liveness 为 HTTP 200。将其标为 P1 可见故障并单独跟进；其原因和实际用户影响范围未定。未归因为 DNS、Cloudflare Tunnel 或本次应用变更。

## 生产版本与文件

- 容器：digest `ghcr.io/zj1123581321/video-transcript-api@sha256:950a1600da44a341b96886dc47e30585b97c227aebbac6f411307a8353ed0487`；imageID `sha256:ca46d3a86cebe92684b2cd1ac7d6f1e311ec51890381160c059b08bf46ab8374`；GIT_SHA `ff92a175243d`；启动时间 `2026-10-03T16:11:07.973881807Z`。
- 状态 running/healthy、restart=0；8000/tcp→宿主 8200；config/data 挂载分别为 `/opt/media/VideoTranscriptAPI/config=>/app/config` 和 `/opt/media/VideoTranscriptAPI/data=>/app/data`；`.deploy-image` digest 与实际容器相同。
- 容器内三份 Git blob 与 ff92 完全相同：`main.py` `8af828469151942a8d7f933ed5bfd4d3684feb0b`；`dialog_segmenter.py` `1d819d661ac5b4109b9cec2c912b2186820e9945`；`api/services/transcription.py` `ff5b09e5bc178c47c820d7278f797da716279e4b`。

## 三个 issue 的限定结论

- **#147 可按原范围关闭**：真实容器 `--check-config` exit 0，`Configuration OK`；JSON 恰含 9 个规定键，每项为 `value`+`source`，实际值/来源均输出在完整派发报告中。仅投影白名单字段；未直接查看或输出 users.json 内容/键及任何凭据值，配置校验命令内部按应用既定流程验证 users.json。
- **#156 可按原范围关闭**：运行容器模块对合成 `A?B!` 输入输出两个片段 `A?`、`B!`，无 LLM 调用。源文件 blob 对齐 ff92。
- **#159 可按原范围关闭**：固定任务 `task_f844d8de96e24c0fb6e42086f498f208` 终态 failed，错误类别 `no_audio_track`，无缓存行或转录侧车；对照任务 `task_b0556bea197b44bf9208e17dd66e875b` success，存在非空 `transcript_funasr.json`（710 bytes）。

## 独立未决现象

- 两个指定 CapsWriter 任务均存在并 failed：`task_cc2eeeebb6894a11a91e25035be021db`（00:22:18–00:24:19 +08:00）和 `task_c3b6e0bee2cc4432abe812b21a6ae8bc`（00:47:18–00:49:19 +08:00），各耗时 121 秒。关联日志白名单字段为 CapsWriter、timeout、RuntimeError；5.55 秒时长来自任务卡输入，生产日志/状态未单独记录该值。
- 没有旧镜像对照，故无法判定既有还是回归；无法证明 `file_seg_duration=25` 因果。建议独立跟踪，不与三个 issue 的关单范围合并。
- Cloudflare 服务 active/running、NRestarts=0，metrics 显示 4 条 HA connections；unit 为 token 托管模式且无 `--config` 参数，所查标准路径未找到本地 ingress 配置，hostname→service 映射未验证。开发机默认/绕过代理两次探针和 n305 探针均为 TLS handshake exit 35、HTTP 000；两端 proxy 相关变量均未设置。不得据此判 DNS/Tunnel 根因。
- 不轮换 token 符合用户明确裁决。未改生产配置/数据，未重启/重部署，未 POST 或重试转录，未操作 issue。OCR 不适用：本卡是只读部署复核而非代码 diff review。

## 收尾记录

- 执行时曾用裸 `python` 启动容器命令，因不在服务虚拟环境而失败；改用 `uv run --no-sync` 后 `--check-config` 成功。pickup 简报脚本同样需用 bash 启动，已纠正。
- SSH curl 参数引号错误曾使一个 GET 输出普通 `/livez` 响应 `{"status":"ok"}`；随后改用 SSH stdin 脚本及 `-o /dev/null` 重测成功。合成分段探针输出了一条 WARNING；其日志 sink 是否持久化未核实。
- 详细生产证据、探针局限、pickup 巡检和偏差记录已写入派发 report.md。
