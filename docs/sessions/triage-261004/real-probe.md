<!-- delegate-outcome: succeeded -->
<!-- delegate-blocked: api_task_id_not_correlatable_to_sdk_server_task_id_on_failure -->
# #166 原样本恢复与真实转录关联诊断

## 执行结果

**诊断卡完成；生产转录实验因关联前提未满足而停止。** 旧报告中的“原样本 URL 不可恢复”和“ASR 服务维护冲突”均已更正：固定历史任务记录恢复出同一公开 WAV；两项活动派发的卡面都排除生产维护，且实际服务端只读核验通过。当前剩余阻塞是：API 任务 ID 没有传入 CapsWriter SDK；SDK 自行生成的服务端任务 ID 在成功结果中可见，但失败异常不保留它。因此提交前无法保证失败时把 API 任务与服务端接收/处理/final 事件一一关联。

- 续修窗口：2026-10-04T03:26:29.458898Z 至本报告归档时间（见归档收据）。
- 新增真实转录请求：0/最多 2；新建生产转录任务：否。
- 累计只读远程调用：16 次 SSH（n305 及 Mac Studio），加首次阶段 2 次 GitHub 只读查询，共 18/20；未超过卡面预算。
- 唯一一次公开样本下载和一次服务健康 GET；未发送转录请求。未改配置、未重启、未部署。
- 最早可证失败边界仍未知；没有以健康检查或代码静态追踪冒充业务复现。

## 样本证据

固定历史任务为 task_cc2eeeebb6894a11a91e25035be021db、task_c3b6e0bee2cc4432abe812b21a6ae8bc（CapsWriter 失败）及 task_b0556bea197b44bf9208e17dd66e875b（FunASR 成功）。先读 task_status schema，再以 SQLite mode=ro、PRAGMA query_only=ON 只查这三个 ID。三行都存在；前两条时间分别为 2026-10-03 16:22:18–16:24:19 和 16:47:18–16:49:19，状态均为 failed；FunASR 行为 success。三条的 url 均为以下同一公开 URL，download_url 均为空，cache_id 均为 None：

https://isv-data.oss-cn-hangzhou.aliyuncs.com/ics/MaaS/ASR/test_audio/asr_example_zh.wav

URL 在进程内检查 HTTPS、无 userinfo、无 fragment、无凭据类 query key，并确认 DNS 地址为 global；只在通过检查后输出 URL。容器内经 HTTPS 下载当前字节一次，逐跳重复同样的 URL 校验，读取上限 8 MiB，并对白名单字段执行 ffprobe：

- HTTP 200；Content-Type: audio/wav；177,572 bytes。
- SHA-256：a1bd32dc78493c123f9625a66deee562aed2895f53fbc39f2cca3be7e6f4f20f。
- WAV 5.546688 秒，单声道 pcm_s16le，16 kHz。
- /tmp/vta166-*.wav 清理后计数为 0。
- 历史任务没有保留输入 hash；以上只证明同 URL 当前下载字节，不能证明与 2026-10-03 的原字节相同。没有把 5.55 秒当作样本身份依据，也没有使用替代样本。

## 服务、维护冲突与健康检查

旧报告把 CapsWriter 仓中一个 running 派发误当成生产维护冲突。定向读取其卡面后确认该任务明确排除部署、ready/merge，工作范围仅为独立 worktree 中的代码、测试和文档；另一项活动卡明确为 synthetic/test-only、无生产访问。活动状态本身不证明生产资源冲突，因此撤销旧判断；未联系或打断执行器。

安全投影核实 n305 上 API 容器为 running，启动时间 2026-10-03T16:11:07.973881807Z，镜像内 GIT_SHA=ff92a175243d。API JSONC 配置中的服务端端口与监听端口匹配；配置主机名和地址未写入报告。唯一一次对配置端点 /health 的 GET 返回 HTTP 200、status=ok、protocol_version=2、model=paraformer、编码 f32le/flac/ogg_opus/s16le；响应仅存于受限临时文件并删除。

Mac Studio 的 6016 端口恰有一个 Python 监听进程，运行时长 02-16:58:39，工作目录 basename 为 capswriter_server_main，部署源码 SHA 6b7a2b82fbc3ebe862250a8804902e5bf37f9211。core/server、core/proxy、sdk 跟踪文件干净；仓库其他区域状态未输出。该部署源码按 task_id 记录接收/最终分段事件，最终结果包含同一 ID。健康检查和部署身份已确认，但这不补足 API 任务与 SDK ID 的失败路径映射。

## 客户端与服务端关联证据

- 本仓 src/video_transcript_api/transcriber/capswriter_client.py:808-822 调用 transcribe_file_sync(file_path, server_url, **sdk_kwargs)；参数为 encoding=flac、配置的 seg_duration/seg_overlap 和按时长计算的可选 deadline_total。调用没有传 API task ID，也没有传 on_progress。这些是代码参数，不是本次真实请求的实测参数。
- 锁定 SDK 858c6b9 的 sdk/capswriter_asr/client.py:338-354 在客户端内部生成 UUID 并写入音频帧；284-297 仅在收到 final result 后把返回的 task_id 放进 Transcript。
- 同文件 18-34,300-318 的 AsrError 只保留 code、message、retryable、recovery_path；接收错误帧时没有保存其中的服务端 ID。连接丢失也只产生不带 task ID 的 AsrError。故失败路径上没有 API task ID → SDK UUID 的映射。
- 部署 SHA 6b7a2b82fbc3ebe862250a8804902e5bf37f9211 的 core/server/connection/ws_recv.py 和 ws_send.py 会记录/回传 SDK UUID。服务端可观测自身 ID，却没有现成字段关联上游 API ID。
- 历史三行 cache_id=None，没有已知产物侧边文件能补出失败请求的 SDK UUID。仅按时间猜配不构成同一任务证据。

因此，只有成功响应可能把 SDK ID 带回调用端；而本卡要诊断的失败路径不能保证映射。按“提交前确认两端可关联”的闸停止，不发送最多两次授权内的真实请求。

## 实际命令与白名单结果

以下保留实际安全命令入口与查询条件；不输出配置全集、私有端点、原始响应、日志或无关用户字段。

    timeout 30 ssh -o ConnectTimeout=10 n305 'docker inspect video-transcript-api --format "{{.State.Status}}|{{.State.StartedAt}}"'
    timeout 30 ssh -o ConnectTimeout=10 n305 'docker exec video-transcript-api printenv GIT_SHA'

输出分别为 running|2026-10-03T16:11:07.973881807Z、ff92a175243d。数据库检查使用：

    timeout 30 ssh -o ConnectTimeout=10 n305 'docker exec -i video-transcript-api python -'

Python 只连接 file:/app/data/cache/cache.db?mode=ro，执行 PRAGMA query_only=ON 和 PRAGMA table_info(task_status)；随后仅对固定三 ID 执行 SELECT task_id,url,status,created_at,completed_at,cache_id FROM task_status WHERE task_id IN (?,?,?)。另一次固定 ID 查询只读 task_id,url,download_url 并对白名单 URL 做公开性校验。结果为 3/3 行、三条 URL 相同、三个 download_url=empty；没有选取标题、用户信息或 raw 行。

样本下载通过 timeout 30 ssh -o ConnectTimeout=10 n305 'docker exec -i video-transcript-api python -' 在容器内执行 Python；健康检查通过 timeout 30 ssh -o ConnectTimeout=10 n305 'docker exec -i video-transcript-api uv run --no-sync python -' 执行。下载实际使用 urllib Request + opener.open(timeout=20)，HTTPS 重定向逐跳校验，流式读取上限 8 MiB；ffprobe argv 为 [ffprobe,-v,error,-show_entries,format=duration,format_name:stream=codec_name,codec_type,sample_rate,channels,-of,json,<本卡临时 WAV>]。白名单 JSON 含 source_url、http_status、content_type、bytes、sha256、format_name、duration_seconds、streams；首次投影的 temp_copy_removed=false 是在 finally 清理前生成，随后独立计数确认 0。健康检查由 API 实际使用的 commentjson 解析器读取配置并构造 /health；curl argv 为 [curl,--silent,--show-error,--max-time,5,-o,<mkstemp under /tmp>,-w,%{http_code},<进程内 URL>]，响应只投影 HTTP 状态、status、protocol version、model、encoding 白名单，最后删除文件。成功投影为 {"api_status":"ok","configured_port_matches_listener":true,"encodings":["f32le","flac","ogg_opus","s16le"],"http_status":"200","model":"paraformer","protocol_version":2,"response_temp_removed":true}。临时 WAV 前缀清理核验命令为：

    timeout 30 ssh -o ConnectTimeout=10 n305 'docker exec video-transcript-api sh -c '\''find /tmp -maxdepth 1 -type f -name "vta166-*.wav" -print | wc -l'\'''

白名单输出为 0。Mac Studio 只读核验限定在 lsof -nP -tiTCP:6016 -sTCP:LISTEN -b、对应 PID 的 ps/lsof -d cwd 投影，以及工作树内 git rev-parse HEAD 和对 core/server core/proxy sdk 的状态查询。没有读取服务日志。代码核对用 git show 858c6b9:sdk/capswriter_asr/client.py、部署 SHA 下 git grep/git show 与本仓行号范围；未修改代码。

## 失败边界、结论与范围

| 边界 | 结论 |
| --- | --- |
| 客户端发出请求 | 未提交，未知 |
| 服务端接收/处理 | 未提交，未知 |
| 服务端返回 final/错误 | 未提交，未知 |
| 客户端接收/解析 | 未提交，未知 |

根因、最早失败边界和最小修复归属均未证实。没有请求/响应、实测 producer 参数、服务端同任务时间线或探针源码；没有跨边界探针，也没有可声称通过的 known-negative 断言。未改代码、依赖或配置；未重启、部署、切换后端或改分段/超时；未读含凭据的原执行器会话日志或 users.json，未改 DNS/tunnel，未删业务数据。临时 WAV 已清理。

## 踩到的坑、闸与偏差

- 旧报告把“URL 不在已有报告”误判成“来源无法恢复”，把另一仓 worktree 的 running 误判成生产维护冲突。按用户续修澄清重新查固定 SQLite 行和两张任务卡后均已纠正。
- 一次健康检查预检先用了不适合 JSONC 的标准 JSON 解析器，在发出 HTTP 请求前失败；改用应用实际的 commentjson 解析器后才执行一次白名单 GET。另一次 lsof 遇到系统卷警告，改用 -b 的同端口定向查询，输出不含警告。
- 卡面允许样本、部署及关联只读预检；这些已完成。真实转录、实测 payload、同一任务时间线因失败路径无法可靠关联而未做。报告存在性命令不表示业务通过；没有运行测试。
- 最贵的一步是沿 API task ID → SDK UUID → 服务端事件/错误返回逐层核查，确认错误对象丢失 UUID。没有用时间相邻、健康状态或版本相同替代任务关联证据。
- 原部署报告中的“客户端约 121 秒后失败”仍只是历史记录；它不能证明是否发出 final，也不能排除 SDK、网络或服务端问题。

## 归档收据

- 分支：card/caps-real-probe-261004；基线 ff92a175243dde36143da867a95e2fdad384e16c。
- 初次诊断起始：2026-10-04T03:14:26.584648Z；续修起始：2026-10-04T03:26:29.458898Z。最终终止时间、提交号、diffstat、工作树状态、远端 SHA 和验收命令记录在派发报告文件。
