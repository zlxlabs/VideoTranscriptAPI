<!-- delegate-outcome: succeeded -->

# CapsWriter 一次性 wire 诊断报告

诊断完成；真实 SDK 调用在服务端完成并分发 final 结果后，客户端调用仍以 `timeout` 结束。诊断任务本身成功取得了真实 SDK UUID 和双端安全时间线。此结果不是 #166 的修复验收，也不证明 API POST 端到端成功。

## 执行结果

- 执行基线：分支 card/caps-wire-probe-261004，base ff92a175243dde36143da867a95e2fdad384e16c。pickup 简报未找到本 worktree 的交接单；未借用其他会话交接单。

- 本卡真实 ASR 调用：**1/最多 2**；没有重试、没有第二次调用。
- 本卡 SSH：**7/最多 12**（n305 2 次，CapsWriter 主机 5 次；含一次只读投影脚本错误后的修正查询）。每次命令均在 30 秒内完成；真实 SDK 观察窗口约 120 秒，外层硬截止 175 秒。
- 没有改应用代码、配置、依赖或运行服务；没有重启、部署、API POST、读 `users.json` 或写其他仓。工作树改动仅限本报告和追加进度。
- 诊断 outcome 为 `succeeded`：实验得到有界、可核对的证据。被诊断的请求 outcome 为 `failed`：SDK 抛出 `AsrError(code=timeout)`。
- 根因只归到已证最早失败边界：SDK 底层 `recv()` 收到同 ID 的 final result 后，`transcribe_file_sync` 未向调用方返回 Transcript，等到 SDK 默认 120 秒预算后报超时。更细的 SDK 内部触发点未证实。

## 复用的基线与当前身份

复用前一轮报告 `前一轮指定的私有安全报告` 中的固定公开样本来源、当前样本 SHA-256、部署版本和服务端日志代码位置；没有重查该报告所述历史任务或重复旧预检。前一轮真实 ASR 调用为 0/2。

- 样本 URL：`https://isv-data.oss-cn-hangzhou.aliyuncs.com/ics/MaaS/ASR/test_audio/asr_example_zh.wav`。
- 历史已记录的当前字节：177,572 bytes，SHA-256 `a1bd32dc78493c123f9625a66deee562aed2895f53fbc39f2cca3be7e6f4f20f`，5.546688 秒，mono `pcm_s16le` 16 kHz。历史原字节没有 hash，本报告不声称与历史请求字节完全相同。
- n305 容器仍为 running，启动时间 `2026-10-03T16:11:07.973881807Z`，`GIT_SHA=ff92a175243d`。
- 容器安装的 SDK `client.py` SHA-256 为 `ff476ad7cd40401b7ed77c7606cb4fc14dc3d529043c29c4714b199c13423cdf`，与本地 SDK checkout 提交 `858c6b975d8bdd2be0e47ac5a36483119894c529` 的该文件哈希相等。
- CapsWriter 6016 listener 唯一，工作目录 basename 为 `capswriter_server_main`，运行代码 SHA `6b7a2b82fbc3ebe862250a8804902e5bf37f9211`。当前 `server_latest.log` 可读；随机未命中 UUID 查询成功且为 0 行。生产前已满足日志可读/可按任务 ID 查询的闸。
- n305 运行配置只投影出 `file_seg_duration=25`、`file_seg_overlap=2`；应用调用固定 `encoding=flac`。探针从容器当前 JSONC 配置内读取服务端 URL 但不输出。`media_duration=None` 且不传 `deadline_total` 是本卡指定的直接 SDK 参数；SDK 默认 idle timeout 为 300 秒，样本对应自动总预算为 120 秒。

## 本地假服务端旁路验证

在本机用同一 SDK 提交和真实 `websockets` 客户端，临时生成 0.2 秒静音 WAV，经 localhost 假服务端接收。旁路包装器只解析发送帧的安全字段，随后把同一个 `payload` 对象原样交给原始 `send()`。假服务端把收到的 WebSocket 应用层 payload 字节与 producer 保存于内存的字节逐字节比较。它不向磁盘保存音频帧。

已知正例为 SDK 自己生成的 UUID `2bae7cd8-6244-48c5-bb28-6840374bae1d`，发送端和假服务端都收到该 ID，整帧长度 11,313 bytes、SHA-256 `012ebab2208791deef7d5d5665b7826419d3528e4f4fcc8f93e4afb11f02bacf`，`application_payload_byte_equal=true`。故意传入不匹配 UUID 和空事件列表均返回 `false`；查询失败返回 `null`（未知），没有把“无事件”混成“查询失败”。假服务端的 synthetic result 仅用于结束本地 SDK 调用，不是真实识别。

以下是该本地验证源码；生产探针源码在下节。运行环境为本机 Python 3.11、`websockets=16.0`、`numpy=2.5.0`，SDK 源码来自前述 pin。

```python
import asyncio, base64, hashlib, json, tempfile, uuid, wave
from pathlib import Path
import websockets
from websockets.asyncio.server import serve
from websockets.datastructures import Headers
from websockets.http11 import Response
from capswriter_asr import client

observed, received, sent_application_payloads = [], [], []
real_connect = websockets.connect

class ObservedSocket:
    def __init__(self, raw): self.raw = raw
    async def send(self, payload, *args, **kwargs):
        data = payload.encode('utf-8') if isinstance(payload, str) else bytes(payload)
        frame = json.loads(data)
        sent_application_payloads.append(data)
        observed.append({k: frame.get(k) for k in ('task_id','source','encoding','is_final','seg_duration','seg_overlap','samples_total') if k in frame} | {'frame_bytes':len(data),'frame_sha256':hashlib.sha256(data).hexdigest(),'media_bytes':len(base64.b64decode(frame['data']))})
        return await self.raw.send(payload, *args, **kwargs)
    async def recv(self): return await self.raw.recv()

class ObservedContext:
    def __init__(self, cm): self.cm = cm
    async def __aenter__(self): return ObservedSocket(await self.cm.__aenter__())
    async def __aexit__(self, *args): return await self.cm.__aexit__(*args)

def observe_connect(*args, **kwargs): return ObservedContext(real_connect(*args, **kwargs))

async def health(connection, request):
    if request.path == '/health':
        body = b'{"protocol_version":2,"encodings":["flac"]}'
        return Response(200, 'OK', Headers([('Content-Type','application/json'),('Content-Length',str(len(body)))]), body)
    return None

async def fake_asr(ws):
    message = await ws.recv()
    data = message.encode('utf-8') if isinstance(message, str) else bytes(message)
    frame = json.loads(data)
    received.append((data, {'task_id':frame.get('task_id'),'event':'received','is_final':frame.get('is_final')}))
    await ws.send(json.dumps({'type':'result','is_final':True,'task_id':frame['task_id'],'text':'','tokens':[],'timestamps':[],'duration':0,'text_accu':''}))

async def main():
    server = await serve(fake_asr, '127.0.0.1', 0, process_request=health)
    port = server.sockets[0].getsockname()[1]
    websockets.connect = observe_connect
    try:
        with tempfile.TemporaryDirectory(prefix='vta166-local-wire-') as td:
            wav_path = Path(td) / 'silence.wav'
            with wave.open(str(wav_path),'wb') as wav:
                wav.setnchannels(1); wav.setsampwidth(2); wav.setframerate(16000); wav.writeframes(b'\0\0'*3200)
            result = await client.transcribe_file(wav_path, f'ws://127.0.0.1:{port}', encoding='flac', seg_duration=25, seg_overlap=2)
            assert result.is_final is True
            assert len(observed) == len(received) == 1
            sent, server_event = received[0]
            assert sent_application_payloads[0] == sent
            assert observed[0]['task_id'] == server_event['task_id']
            assert str(uuid.UUID(observed[0]['task_id'])) == observed[0]['task_id']
            events = [server_event]
            def correlated(expected, state, records):
                if state != 'ok': return None
                return any(e.get('task_id') == expected for e in records)
            mismatch = str(uuid.uuid4())
            negatives = {'empty_events':correlated(observed[0]['task_id'],'ok',[]),'mismatched_id':correlated(mismatch,'ok',events),'query_failure':correlated(observed[0]['task_id'],'error',events)}
            assert negatives == {'empty_events':False,'mismatched_id':False,'query_failure':None}
            print(json.dumps({'local_fake_server':'passed','sdk_source_commit':'858c6b975d8bdd2be0e47ac5a36483119894c529','sdk_task_id_observed':observed[0]['task_id'],'producer_payload_safe':observed[0],'server_received_payload':{'task_id':server_event['task_id'],'event':server_event['event'],'is_final':server_event['is_final'],'frame_bytes':len(sent),'frame_sha256':hashlib.sha256(sent).hexdigest()},'application_payload_byte_equal':True,'known_negative':negatives},sort_keys=True))
    finally:
        websockets.connect = real_connect
        server.close(); await server.wait_closed()

asyncio.run(main())
```

本地白名单 stdout：

```json
{"application_payload_byte_equal": true, "known_negative": {"empty_events": false, "mismatched_id": false, "query_failure": null}, "local_fake_server": "passed", "producer_payload_safe": {"encoding": "flac", "frame_bytes": 11313, "frame_sha256": "012ebab2208791deef7d5d5665b7826419d3528e4f4fcc8f93e4afb11f02bacf", "is_final": true, "media_bytes": 8323, "samples_total": 3200, "seg_duration": 25, "seg_overlap": 2, "source": "file", "task_id": "2bae7cd8-6244-48c5-bb28-6840374bae1d"}, "sdk_source_commit": "858c6b975d8bdd2be0e47ac5a36483119894c529", "sdk_task_id_observed": "2bae7cd8-6244-48c5-bb28-6840374bae1d", "server_received_payload": {"event": "received", "frame_bytes": 11313, "frame_sha256": "012ebab2208791deef7d5d5665b7826419d3528e4f4fcc8f93e4afb11f02bacf", "is_final": true, "task_id": "2bae7cd8-6244-48c5-bb28-6840374bae1d"}}
```

## 一次真实调用与安全时间线

生产调用直接在 `video-transcript-api` 容器中运行 SDK；不是 API POST/端到端验收。启动前再次断言 API SHA、SDK 文件 hash 与生产前置检查一致。脚本先将唯一公开样本下载到权限为 0700 的唯一临时目录，逐个 HTTPS 重定向目标做公开 URL 校验，受限 8 MiB，核对完整 SHA/长度/ffprobe 格式后才调用 SDK；`finally` 删除临时目录。外层 SSH 观察窗口 `timeout 175`，每次 SDK 只调用一次。

实际样本 SHA 与已记录值完全相等：177,572 bytes，`audio/wav`，5.546688 秒，单声道 `pcm_s16le` 16 kHz。调用参数：`encoding=flac`、`seg_duration=25`、`seg_overlap=2`，不传 `deadline_total` 或 `idle_timeout`；SDK 对该样本的自动总预算为 120 秒，默认 idle timeout 为 300 秒。旁路包装器在 `send` 前读取 `task_id` 和帧安全摘要，把原始 `payload` 原样交给原始 `send`；不改 UUID、序列化、帧内容或预算。

实发任务 ID：`c590f7c3-482b-4d9e-b7c7-f7b8e6a38abc`。producer 安全 payload 为：

```json
{"encoding":"flac","frame_bytes":125883,"frame_sha256":"448f4aa01c665df5bbe0e95cb80a0bddc9f8f19e600cc323857dcbfc6c930bb8","is_final":true,"media_bytes":94250,"samples_total":88747,"seg_duration":25,"seg_overlap":2,"source":"file","task_id":"c590f7c3-482b-4d9e-b7c7-f7b8e6a38abc","time_utc":"2026-10-04T04:06:45.241+00:00"}
```

该摘要保留的是 SDK 实际 producer 帧的白名单投影与完整应用层帧 hash；base64 媒体字段没有写入报告或日志。客户端 `send` 返回成功，发送 1 帧。以下是按这个 UUID 固定过滤后，从服务端日志中投影的事件；实际读了 6 个 `server_latest.log*` 文件，只输出时间、级别、阶段、来源行、终态状态和错误码：

| 服务端日志本地时间 | 级别 | 阶段 | 来源 | 安全字段 |
| --- | --- | --- | --- | --- |
| 2026-10-04 12:06:44.755 | INFO | receive_complete | `ws_recv.py:315` | 无错误码 |
| 2026-10-04 12:06:44.755 | DEBUG | final_submit | `ws_recv.py:344` | 无错误码 |
| 2026-10-04 12:06:44.870 | INFO | task_end | `state.py:276` | status=done，未设错误码 |
| 2026-10-04 12:06:44.870 | DEBUG | result_dispatched | `ws_send.py:204` | 无错误码 |

客户端时间为 UTC；SDK `recv()` 旁路观测到与实发 UUID 相同的终态结果帧。随后 SDK 调用仍超时：

| UTC 时间 | 阶段 | 结果 |
| --- | --- | --- |
| 2026-10-04T04:06:45.157Z | SDK 调用开始 | 一次调用，未重试 |
| 2026-10-04T04:06:45.241Z | client send 开始 | task_id=`c590f7c3-482b-4d9e-b7c7-f7b8e6a38abc`，is_final=true |
| 2026-10-04T04:06:45.246Z | client send 返回 | 成功 |
| 2026-10-04T04:06:45.414Z | client recv 收到帧 | type=result，is_final=true，同一 task_id；不记录识别正文 |
| 2026-10-04T04:08:45.338Z | SDK 调用结束 | `AsrError(code=timeout)`；调用耗时 120.152 秒，未返回 Transcript |

生产前随机 UUID 查询是 `query_status=ok`、0 命中；真实 UUID 查询也是 `query_status=ok`，6 个日志文件中共 4 条匹配事件。没有把“无事件”当成查询失败；真实查询没有错误码，也没有匹配到服务端 error 事件。服务器日志时间保留其本地原值，不猜偏移量。临时样本文件已删除。client result 对象没有返回，因此没有正文，也没有可报告的正文长度。

## 结论与边界

已证发送帧含真实 SDK UUID，服务端以同一 ID 接收最终音频、提交最终片段、记录 `task_end status=done` 并分发结果；客户端底层 `recv()` 收到相同 ID 的 `is_final=true` 结果帧。最早可证失败点在客户端收到该帧之后、SDK 调用返回 Transcript 之前。当前证据不能区分 SDK final-frame 处理、异步任务收尾或预算监督中的具体原因，故不宣称根因已定位到更细函数，也不做修复。

此实验没有经过 FastAPI、业务重试、下载器调用、缓存或用户认证。任务卡要求直 SDK probe 使用 `media_duration=None`；当前应用代码则会从 `actual_downloader.last_media_duration` 读取可选时长，并在有值时计算 `deadline_total`。因此本报告准确记录本次直 SDK 实际参数，不把它冒称为 API POST 的端到端参数复现。该偏差不影响 UUID 与双端事件关联，但限制对 API 调用超时参数的外推。

本次短样本当前下层路线也没有成功返回到 SDK 调用方；服务端确实完成并分发了结果。它不等于历史失败已修复，或 API 端到端通过。没有运行项目测试；卡面 Verify-Command 只验证诊断报告存在。

## 生产探针完整源码

以下源码就是在 n305 容器执行的单次探针。它不输出服务端 URL、原始日志、媒体正文或转录内容；错误只投影类别和白名单错误码。配置 URL 仅在内存中交给 SDK。

```python
import base64, hashlib, ipaddress, json, os, re, shutil, socket, subprocess, tempfile, time, uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
import commentjson, websockets
from capswriter_asr import client, transcribe_file_sync

EXPECTED_API_SHA = 'ff92a175243d'
EXPECTED_SDK_SHA = 'ff476ad7cd40401b7ed77c7606cb4fc14dc3d529043c29c4714b199c13423cdf'
EXPECTED_SAMPLE_SHA = 'a1bd32dc78493c123f9625a66deee562aed2895f53fbc39f2cca3be7e6f4f20f'
SOURCE_URL = 'https://isv-data.oss-cn-hangzhou.aliyuncs.com/ics/MaaS/ASR/test_audio/asr_example_zh.wav'
SENSITIVE_KEYS = {'token','access_token','api_key','key','signature','sig','auth','authorization','credential','password','secret','x_amz_credential','x_amz_signature','x_amz_security_token'}

def now(): return datetime.now(timezone.utc).isoformat(timespec='milliseconds')
def safe_uuid(value):
    try: return str(uuid.UUID(value)) if value else None
    except (ValueError, TypeError, AttributeError): return None

def validate_public_url(value):
    parts = urlsplit(value)
    if parts.scheme != 'https' or not parts.hostname or parts.username or parts.password or parts.fragment:
        raise ValueError('public_url_check_failed')
    keys = [k.lower().replace('-', '_') for k, _ in parse_qsl(parts.query, keep_blank_values=True)]
    if any(k in SENSITIVE_KEYS or any(x in k for x in ('token','secret','password')) for k in keys):
        raise ValueError('public_url_credential_key')
    try: addresses = [ipaddress.ip_address(parts.hostname)]
    except ValueError: addresses = [ipaddress.ip_address(item[4][0]) for item in socket.getaddrinfo(parts.hostname, parts.port or 443, type=socket.SOCK_STREAM)]
    if not addresses or not all(a.is_global for a in addresses): raise ValueError('public_url_not_global')

class PublicRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        validate_public_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)

async def unused_health(*args): return None

class ObservedSocket:
    def __init__(self, raw, observations, retained): self.raw, self.observations, self.retained = raw, observations, retained
    async def send(self, payload, *args, **kwargs):
        data = payload.encode('utf-8') if isinstance(payload, str) else bytes(payload)
        frame = json.loads(data)
        record = {k: frame[k] for k in ('source','encoding','is_final','seg_duration','seg_overlap','samples_total') if k in frame}
        record.update({'task_id':safe_uuid(frame.get('task_id')), 'frame_bytes':len(data), 'frame_sha256':hashlib.sha256(data).hexdigest(), 'media_bytes':len(base64.b64decode(frame['data'])), 'event':'client_send_started', 'time_utc':now()})
        self.retained.append(data)
        self.observations.append(record)
        await self.raw.send(payload, *args, **kwargs)
        self.observations.append({'event':'client_send_returned','time_utc':now(),'task_id':record['task_id']})
    async def recv(self):
        try: message = await self.raw.recv()
        except Exception as exc:
            self.observations.append({'event':'client_receive_error','time_utc':now(),'error_class':type(exc).__name__})
            raise
        raw = message.encode('utf-8') if isinstance(message, str) else bytes(message)
        try: frame = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError): frame = {}
        kind = frame.get('type') if frame.get('type') in {'result','error'} else 'other'
        record = {'event':'client_receive_frame','time_utc':now(),'message_type':kind,'task_id':safe_uuid(frame.get('task_id')),'is_final':frame.get('is_final') is True}
        if kind == 'error' and re.fullmatch(r'[a-z0-9_]{1,64}', str(frame.get('code',''))): record['error_code'] = frame['code']
        self.observations.append(record)
        return message

class ObservedContext:
    def __init__(self, cm, observations, retained): self.cm, self.observations, self.retained = cm, observations, retained
    async def __aenter__(self): return ObservedSocket(await self.cm.__aenter__(), self.observations, self.retained)
    async def __aexit__(self, *args): return await self.cm.__aexit__(*args)

def run():
    if os.environ.get('GIT_SHA') != EXPECTED_API_SHA: raise RuntimeError('api_build_mismatch')
    sdk_path = Path(__import__('inspect').getsourcefile(client))
    if hashlib.sha256(sdk_path.read_bytes()).hexdigest() != EXPECTED_SDK_SHA: raise RuntimeError('sdk_pin_mismatch')
    config = commentjson.loads(Path('/app/config/config.jsonc').read_text(encoding='utf-8'))
    caps = config.get('capswriter', {})
    seg_duration = caps.get('file_seg_duration', 25)
    seg_overlap = caps.get('file_seg_overlap', 2)
    server_url = caps.get('server_url')
    parts = urlsplit(server_url or '')
    if parts.scheme not in {'ws','wss'} or not parts.hostname: raise RuntimeError('server_url_invalid')
    validate_public_url(SOURCE_URL)
    work = Path(tempfile.mkdtemp(prefix='vta166-wire-probe-'))
    sample = work / 'sample.wav'
    result = {'status':'precondition_failed','api_git_sha':EXPECTED_API_SHA,'sdk_pin_match':True,'parameters':{'encoding':'flac','seg_duration':seg_duration,'seg_overlap':seg_overlap,'media_duration':None,'deadline_total_passed':False,'idle_timeout_passed':False,'sdk_default_idle_timeout_seconds':300,'automatic_retry':False}}
    try:
        opener = build_opener(PublicRedirect())
        request = Request(SOURCE_URL, headers={'User-Agent':'VideoTranscriptAPI-wire-probe'})
        with opener.open(request, timeout=20) as response:
            validate_public_url(response.geturl())
            body = response.read(8 * 1024 * 1024 + 1)
            sample_projection = {'http_status':response.status,'content_type':response.headers.get('Content-Type','').split(';',1)[0],'bytes':len(body),'sha256':hashlib.sha256(body).hexdigest()}
        if sample_projection['http_status'] != 200 or sample_projection['bytes'] != 177572 or sample_projection['sha256'] != EXPECTED_SAMPLE_SHA:
            raise RuntimeError('sample_identity_mismatch')
        sample.write_bytes(body)
        probe = subprocess.run(['ffprobe','-v','error','-show_entries','format=duration,format_name:stream=codec_type,codec_name,sample_rate,channels','-of','json',str(sample)],capture_output=True,text=True,check=True,timeout=8)
        probe_data = json.loads(probe.stdout)
        fmt = probe_data['format']
        streams = probe_data['streams']
        duration = float(fmt['duration'])
        if fmt.get('format_name') != 'wav' or len(streams) != 1 or streams[0].get('codec_type') != 'audio' or streams[0].get('codec_name') != 'pcm_s16le' or streams[0].get('sample_rate') != '16000' or streams[0].get('channels') != 1:
            raise RuntimeError('sample_format_mismatch')
        sample_projection.update({'format_name':fmt.get('format_name'),'duration_seconds':duration,'streams':[{'codec_type':s.get('codec_type'),'codec_name':s.get('codec_name'),'sample_rate':s.get('sample_rate'),'channels':s.get('channels')} for s in streams]})
        result['sample'] = sample_projection
        result['effective_sdk_default_total_deadline_seconds'] = max(120.0, duration + 60.0)
        result['client_call_started_utc'] = now()
        observations, retained = [], []
        real_connect = websockets.connect
        def observed_connect(*args, **kwargs): return ObservedContext(real_connect(*args, **kwargs), observations, retained)
        websockets.connect = observed_connect
        client_error = None
        transcript = None
        started = time.monotonic()
        try: transcript = transcribe_file_sync(sample, server_url, encoding='flac', seg_duration=seg_duration, seg_overlap=seg_overlap)
        except Exception as exc:
            client_error = {'error_class':type(exc).__name__}
            if re.fullmatch(r'[a-z0-9_]{1,64}', str(getattr(exc,'code',''))): client_error['error_code'] = exc.code
        finally:
            websockets.connect = real_connect
        elapsed = round(time.monotonic() - started, 3)
        result.update({'status':'probe_completed','client_outcome':'succeeded' if client_error is None else 'failed','client_error':client_error,'client_call_ended_utc':now(),'client_elapsed_seconds':elapsed,'actual_send_count':len([e for e in observations if e.get('event') == 'client_send_started']),'producer_payload_safe':observations[0] if observations else None,'client_timeline':observations,'transcript_projection':None if transcript is None else {'task_id':safe_uuid(transcript.task_id),'is_final':transcript.is_final,'text_chars':len(transcript.text or ''),'text_accu_chars':len(transcript.text_accu or '')}})
        if observations and observations[0].get('task_id') is None: result['observer_uuid_valid'] = False
        else: result['observer_uuid_valid'] = bool(observations)
        return result
    finally:
        shutil.rmtree(work)
        result['sample_temp_removed'] = not work.exists()

try:
    output = run()
except Exception as exc:
    output = {'status':'stopped_before_or_during_probe','error_class':type(exc).__name__}
    if str(exc) in {'api_build_mismatch','sdk_pin_mismatch','sample_identity_mismatch','sample_format_mismatch','server_url_invalid'}: output['stop_reason'] = str(exc)
print(json.dumps(output, sort_keys=True))
```

真实探针唯一 stdout（完整原始 frame body、URL endpoint、转录内容均未输出）：

```json
{"actual_send_count": 1, "api_git_sha": "ff92a175243d", "client_call_ended_utc": "2026-10-04T04:08:45.338+00:00", "client_call_started_utc": "2026-10-04T04:06:45.157+00:00", "client_elapsed_seconds": 120.152, "client_error": {"error_class": "AsrError", "error_code": "timeout"}, "client_outcome": "failed", "client_timeline": [{"encoding": "flac", "event": "client_send_started", "frame_bytes": 125883, "frame_sha256": "448f4aa01c665df5bbe0e95cb80a0bddc9f8f19e600cc323857dcbfc6c930bb8", "is_final": true, "media_bytes": 94250, "samples_total": 88747, "seg_duration": 25, "seg_overlap": 2, "source": "file", "task_id": "c590f7c3-482b-4d9e-b7c7-f7b8e6a38abc", "time_utc": "2026-10-04T04:06:45.241+00:00"}, {"event": "client_send_returned", "task_id": "c590f7c3-482b-4d9e-b7c7-f7b8e6a38abc", "time_utc": "2026-10-04T04:06:45.246+00:00"}, {"event": "client_receive_frame", "is_final": true, "message_type": "result", "task_id": "c590f7c3-482b-4d9e-b7c7-f7b8e6a38abc", "time_utc": "2026-10-04T04:06:45.414+00:00"}], "effective_sdk_default_total_deadline_seconds": 120.0, "observer_uuid_valid": true, "parameters": {"automatic_retry": false, "deadline_total_passed": false, "encoding": "flac", "idle_timeout_passed": false, "media_duration": null, "sdk_default_idle_timeout_seconds": 300, "seg_duration": 25, "seg_overlap": 2}, "producer_payload_safe": {"encoding": "flac", "event": "client_send_started", "frame_bytes": 125883, "frame_sha256": "448f4aa01c665df5bbe0e95cb80a0bddc9f8f19e600cc323857dcbfc6c930bb8", "is_final": true, "media_bytes": 94250, "samples_total": 88747, "seg_duration": 25, "seg_overlap": 2, "source": "file", "task_id": "c590f7c3-482b-4d9e-b7c7-f7b8e6a38abc", "time_utc": "2026-10-04T04:06:45.241+00:00"}, "sample": {"bytes": 177572, "content_type": "audio/wav", "duration_seconds": 5.546688, "format_name": "wav", "http_status": 200, "sha256": "a1bd32dc78493c123f9625a66deee562aed2895f53fbc39f2cca3be7e6f4f20f", "streams": [{"channels": 1, "codec_name": "pcm_s16le", "codec_type": "audio", "sample_rate": "16000"}]}, "sample_temp_removed": true, "sdk_pin_match": true, "status": "probe_completed", "transcript_projection": null}
```

## 过滤日志查询源码

下面是可单独运行的完整只读过滤器。它固定按传入 task ID 筛行，只输出时间、级别、阶段、来源位置、终态状态和错误码；读失败会输出 query_status=error，与成功但零命中分开。

~~~python
import glob, json, pathlib, re, sys

log_dir = pathlib.Path(sys.argv[1])
task_id = sys.argv[2]
events = []
try:
    for name in sorted(glob.glob(str(log_dir / 'server_latest.log*'))):
        with open(name, 'r', encoding='utf-8', errors='replace') as stream:
            for line in stream:
                if task_id not in line:
                    continue
                ts = re.match(r'^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\.\d+)', line)
                level = re.search(r'\b(DEBUG|INFO|WARNING|ERROR|CRITICAL)\b', line)
                loc = re.search(r'\[\s*([^:\]]+):(\d+)', line)
                file_name = loc.group(1).strip() if loc else None
                line_no = int(loc.group(2)) if loc else None
                if file_name == 'ws_recv.py' and line_no == 315: phase = 'receive_complete'
                elif file_name == 'ws_recv.py' and line_no == 344: phase = 'final_submit'
                elif file_name == 'state.py' and line_no == 276: phase = 'task_end'
                elif file_name == 'ws_send.py' and line_no == 204: phase = 'result_dispatched'
                elif level and level.group(1) in {'ERROR','CRITICAL'}: phase = 'error'
                else: phase = 'task_id_event'
                event = {'time_local':ts.group(1) if ts else None,'level':level.group(1) if level else None,'phase':phase,'source_file':file_name,'source_line':line_no}
                code = re.search(r'\bcode=([a-z0-9_-]{1,64})', line)
                if code and code.group(1) != '-': event['error_code'] = code.group(1)
                status = re.search(r'\bstatus=(done|failed)\b', line)
                if status: event['task_status'] = status.group(1)
                events.append(event)
    print(json.dumps({'task_id':task_id,'query_status':'ok','event_count':len(events),'events':events}, sort_keys=True))
except OSError as exc:
    print(json.dumps({'task_id':task_id,'query_status':'error','error_class':type(exc).__name__}, sort_keys=True))
    raise SystemExit(2)
~~~

## 坑、闸、偏差与最贵的一步

- pickup 巡检摘要为 summary: orphan 0 owned 0 unattributable 0 too-new 0 recent-7d 0 stale-over-7d 0 missing_ledger_repos 0；巡检项未展开，需要时跑 /worksite-audit。memory 探针原行是“memory 巡检报告不可用：memory_dir_mismatch（本机 latest.json）”；需运行 memory_doctor_run.sh <agent-config目录> 重建。

- 先本地正例/负例验证关联判据，再生产调用；随机 UUID 的空命中证明日志查询通道可读，不用“看到任何日志”冒充匹配。
- 第一次 Mac `lsof -b` 因系统卷扫描限制输出了大量 warning；输出被工具截断，未用于结论。后续在远端把该命令的 stderr 重定向到 `/dev/null`，只取目标 listener PID/CWD，投影结果不含这些警告。
- 一次补充日志投影脚本因正则写错在读取日志后退出；前一条按 UUID 查询已经成功。修正后按相同 ID 只读查询成功，输出仍只有白名单字段。该脚本问题不导致 ASR 重发。
- 首次推送被仓库公开内容扫描器拦下，因为报告引用了本机报告/巡检路径；已将仓内文档改为无本机绝对路径表述，私有派发报告保留详细路径。
- 探测本机 SDK 时误用 `uv run --no-sync`，它新建了空 `.venv` 后因 SDK 未安装而导入失败；立即删除这次新建的工作树 `.venv`，之后从现成 SDK 源码树加载，没有安装依赖。工作树最终状态复核干净（文档提交前）。
- `docs/project-memory.md` 在本 worktree 不存在；部署事实复用用户卡片和 agent memory 索引命中的上一轮安全报告，没有臆测补写。
- 卡片的 `media_duration=None` 用于本次直接 SDK 探针；当前 API 代码可从 downloader 取得时长并据此算 deadline。因为没有走 API 下载器，本报告没有验证该值会否在同样 URL 的 API 流程中非空，作为范围偏差明确保留。
- 最贵的一步是等待唯一 SDK 请求走完默认自动总预算：服务端在约 0.3 秒内收到并完成任务，但调用方直到 120.152 秒才返回 timeout。没有为“多一次可能解释更多”消耗第二次真实调用。
- 未能判定任何 CI 继承红/新红：本卡不跑 CI 或测试，派发卡基线记录 `gh api request failed`。

## 交付

- 文档新增行数超过 target 200、低于 hard 600；增量主要是任务要求保留的本地与生产完整探针源码、producer 安全 payload 和白名单输出。
- 仅有本报告和 `progress/wire-probe-progress.md` 两个文档在提交范围内；测试未运行。
- 验收命令：`test -s docs/sessions/triage-261004/wire-probe.md`。
- 本地 fake server 断言属于任务要求的观察器契约验证，不是项目测试套件。
- 最终 commit、push 远端 SHA、diff 行数、`git diff --check` 与工作树收据在本卡最终回复中给出。
