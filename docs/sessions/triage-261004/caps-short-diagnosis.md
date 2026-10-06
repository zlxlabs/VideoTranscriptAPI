<!-- delegate-outcome: succeeded -->

# #166 CapsWriter 短音频失败边界诊断（只读）

派发：`dlg-20261004-023618-c714a0`（修订 `dlg-20261004-030118-009132`）；代码基线：`ff92a175243dde36143da867a95e2fdad384e16c`（= 生产运行版本）；Verify-Mode `diagnostic-report`。

> **修订说明（本版）**：上一版（`86e9d55a`）在归因上过强，本版按主脑审阅收窄——
> ① SDK 文件 sha256 一致**只证明版本一致**，不能排除 SDK 侧缺陷；
> ② 本地假服务端「永不回复」**只证明客户端预算行为**，不能证明生产服务端没发 final；
> ③ 「分段缺陷」「时限太短」由「已排除」降为**未证实**（`is_final=true` 单帧只说明 SDK 发送侧，服务端如何做短尾分段未被观测）；
> ④ 删除「只有该 wav 撞 120 秒下限」等基于同期任务耗时的推断——那些是**处理耗时**不是媒体时长，没有解码后时长就算不出它们的预算值；
> ⑤ `GenericDownloader.download_file` 不调用 `_validate_media_file` **不等于本仓从未 ffprobe**（共享音轨准入等路径可能探测过但不写 `last_media_duration`）；只保留「`last_media_duration` 缺失」这一条已知；
> ⑥ 时间线中的媒体标题/文件名全部匿名化；
> ⑦ 墙钟按实际执行起止纠正为约 24 分钟（上一版「约 50 分钟、超软限」的说法不成立，已撤回）；
> ⑧ 探针源码与真实输出从 `/tmp` 迁入本文附录（附录 A/B）。

## 一句话结论

**客户端侧已证**：这次失败发生在客户端计时边界——本仓在拿不到 `last_media_duration` 时不传 `deadline_total`，SDK 用自动预算（`client.py:418` 的 `max(120, 解码后音频时长 + 60)`），到点后客户端主动放弃并显式报 `AsrError(code=timeout, 「转录超过自动预算：远端转录阶段超时」)`。
**未证**：真实网络传输、生产服务端的接收与终态、以及「服务端为什么没有在该预算内给出结果」。根因保持未知。

## 一、事实（真实生产调用观测）

时间均为日志本地时间（UTC+8），UTC = 本地 − 8h。媒体身份一律匿名化。

| 观测 | 值 | 出处 |
| --- | --- | --- |
| 输入文件（样本 A，两次失败同一文件） | wav，直链下载成功，0.17 MB | `app.log` `downloaders.generic:download_file:717` |
| 下载器 | `downloaders.generic` | 同上 |
| 时限传递 | `transcription_deadline duration=unknown fallback=sdk_auto` | `capswriter_client:_transcription_deadline:141`，两个失败任务各一条 |
| 终态消息 | `code=timeout, 原因: 转录超过自动预算：远端转录阶段超时` | `capswriter_client:log:648` |
| 尝试次数 | `尝试 1/5`，无重试 | 同上 |
| 转录阶段耗时 | `120190ms` / `120183ms` | `perf_tracker:track:68` |
| 任务时间 | 样本 A 第一次 00:22:18→00:24:19；第二次 00:47:18→00:49:19 | 同上 |
| 容器内 SDK 文件指纹 | `capswriter_asr/client.py` sha256 `ff476ad7…34cdf`，与本仓 pin `858c6b9` 同名文件 sha256 相同 | `docker exec sha256sum` vs 本地 `sha256sum` |
| ASR 端点与探活 | `ws://<ASR_HOST>:6016`（内网端点，公开仓脱敏），GET `/health` → `protocol_version=2, role=server, encodings=[f32le,s16le,flac,ogg_opus], model=paraformer` | 生产 config 单键 grep + n305 单 URL GET |

**关于 SDK 指纹的正确读法**：它只回答「生产跑的 SDK 代码与本仓 pin 的同一份」——排除的是「版本漂移」这一个解释。它**不能**排除 SDK 侧存在缺陷（例如帧构造、`is_final` 语义或预算算法的正确性），因为本卡没有对 SDK 做行为层面的正确性审计。

**关于 `/health` 的正确读法**：它只证明该时刻进程活着且声明协议 v2。它**不能**证明任何具体任务的服务端终态，也不能证明当时该进程就是处理这两个任务的实例（多实例/重启可能）。

## 二、本仓这一侧已经走通的路径（有观测支撑）

```
直链 wav → GenericDownloader.download_file（不写 last_media_duration）
        → transcription.py: getattr(actual_downloader, "last_media_duration", None) → None
        → transcriber.transcribe(audio, base)            # 两参形状，media_duration 不传
        → capswriter_client._transcription_deadline(None) → None + 日志 fallback=sdk_auto
        → 不带 deadline_total 调 transcribe_file_sync(encoding=flac, seg_duration=25, seg_overlap=2)
        → SDK client.py:418 set_deadline(max(120, 解码后PCM时长+60)) → 5.55s 落在 120 秒下限
        → deadline_watch 到点 → AsrError(timeout, 转录超过自动预算：远端转录阶段超时), retryable=False
        → 只试一次，任务 failed
```

**这里唯一可以说的**是：失败发生在客户端预算到点的那一刻，且该预算值由「无时长 → 自动预算下限」这一条决定。
**这里不能说的**是：服务端在此期间做了什么。

## 三、未知（三态保留，不合并）

| 层 | 状态 | 依据 |
| --- | --- | --- |
| 客户端预算与失败上报 | **已证** | 生产日志 + 本地 synthetic 探针逐字复现（见第五节、附录 B） |
| 真实网络传输（帧是否真的到达生产服务端、是否中途丢帧/断连） | **未知** | 生产无抓包、服务端无日志观测；本地探针的「实收帧」是本地假服务端的观测，对生产不成立 |
| 生产服务端对该任务的接收与终态（是否收到、是否处理、何时回 final、是否回了但客户端没等到） | **未知** | `/health` 不构成任务级证据；本卡禁止向生产 ASR 提交音频 |
| 「时限太短」 | **未证实**（既不能断言成立，也不能断言排除） | 缺「服务端实际处理这段音频需要多久」这一项观测 |
| 「短音频分段缺陷（5.55s 与 `file_seg_duration=25` 的关系）」 | **未证实**（同上） | 探针只看到 SDK **发送侧**是单帧 `is_final=true`；服务端如何切分、如何决定 final 时机**完全未被观测**，不能据此排除服务端侧的短尾/分段问题 |
| 输入音频真实身份与时长 | **未证实** | 见第六节 |

## 四、同期任务时间线（只读投影，输入已匿名）

**读法限制（重要）**：`perf` 的「转录阶段耗时」是**处理耗时**，不是媒体时长。没有 SDK 解码后时长（`samples_total/16000`）就无法算出这些任务各自的自动预算值，因此本表**不能**用来断言「谁撞了 120 秒下限」「谁没撞」「为什么只有某个输入失败」。上一版基于此表的强推断已删除。

| 本地时间 | 输入（匿名） | 时限传递 | 终态 | 转录阶段耗时 |
| --- | --- | --- | --- | --- |
| 10-03 18:14:58 | 样本 B（前一日任务） | `duration=93.09 value=492.4`（显式 `deadline_total`） | 不在本次查询范围 | — |
| 10-03 19:31→19:42 | 样本 C（m4a） | `duration=unknown` → sdk_auto | 成功 | — |
| 10-03 23:11→23:17 | 样本 D（m4a） | `duration=unknown` → sdk_auto | 成功 | — |
| 10-04 00:22:18→00:24:19 | **样本 A**（wav，0.17 MB） | `duration=unknown` → sdk_auto | **failed timeout** | 120190ms |
| 10-04 00:25:46→00:31:33 | 样本 E（m4a） | （窗口内） | 成功 | — |
| 10-04 00:31:19→05:25:00 | 样本 F（mp4，494 MB） | `duration=unknown` → sdk_auto | **failed timeout** | 17621038ms |
| 10-04 00:31:24→00:36:45 | 样本 G（m4a） | `duration=unknown` → sdk_auto | 成功 | 321208ms |
| 10-04 01:23:17→01:28:51 | 样本 H（m4a） | （窗口内） | 成功 | — |

这张表唯一支持的结论很窄：**同一份 app.log 里，既有走 sdk_auto 成功的任务，也有 sdk_auto 失败的任务，且输入形态不止一种**。它不支持任何关于预算值大小、短音频特殊性的因果推断。

## 五、本地 synthetic 探针（不连生产 ASR）

环境：worktree 内 `uv sync --offline` 建的 `.venv`，Python 3.11，SDK pin `858c6b9`；ffmpeg 生成 5.55s/16kHz/mono 正弦 wav（sha256 `c08fd13b…6aca7`，177678 bytes，ffprobe `5.550000`）。源码与真实输出见附录 A/B。

**边界声明**：
- 这些探针是 **synthetic** 的：输入是自造正弦波不是样本 A，服务端是本地假实现不是生产 ASR。**不构成生产复现**。
- 探针 A 证明的命题：「本仓传给 SDK 的 kwargs 在各种 `media_duration` 取值下长什么样」+「`GenericDownloader.download_file` 之后 `last_media_duration` 仍为 None」。
- 探针 B 证明的命题：「SDK 在**服务端永不回复**这一合成条件下，按自动预算/显式预算到点放弃，并给出该文案」。它**不**证明生产服务端没发 final——假服务端是被人为写成永不回复的。
- 唯一一次真实外网访问：探针 A 早期版本误 mock 了 `requests.get`，而 `GenericDownloader` 走 `_safe_request` 钉 IP 路径，导致真的向合成域名 `example.com` 发了 3 次请求（全部 404，无凭据、无生产数据）。修正版 mock 的是 `_safe_request`，本次重跑未产生任何网络请求。因此本卡**不能**声称「全程无网络」。

结果摘要（明细见附录 B）：

- `media_duration=None` → kwargs 无 `deadline_total`；
- `media_duration=0.0` → `deadline_total=120.0`（数值上与自动预算下限相同的另一条路径；生产日志是 `duration=unknown` 而非 `duration=0.00`，故本例走的是前者）；
- `media_duration=5.55` → `deadline_total=142.2`；`93.08898` → `492.35592`；
- `GenericDownloader.download_file` 真实执行后 `last_media_duration is None`，且其源码内无 `_validate_media_file` 调用；
- 假服务端条件下：不传 `deadline_total` → 120.2s 后 `AsrError(timeout, 转录超过自动预算：远端转录阶段超时, retryable=False)`；传 `142.2` → 142.2s 后 `AsrError(timeout, 转录超过deadline_total：…)`。

## 六、输入样本可复核程度：低

生产日志只留下「wav、0.17 MB」两项；该文件已随任务结束从 `data/temp` 清理（`ls data/temp` 为空）。5.55s 来自验收输入资料，**任务表与日志都没有媒体时长字段**。本地探针的 5.55s wav 是自造正弦波，只用于复现预算行为，不能替代原样本。因此「样本 A 确实是 5.55s 的中文语音」这一前提本身也属于未证实项。

## 七、修复归属建议（不下定论）

- **本仓（可能，但需要证据支撑才成立）**：`last_media_duration` 缺失使这次落回自动预算下限。**不能**据此断言「服务端稍晚回包就必须改本仓」——即使把时长补上、显式按 5.55s 传 `deadline_total`，预算也只有 **142.2 秒**；若服务端实际需要的延迟大于 142.2 秒，改本仓同样会失败。要判定归属，至少需要：样本 A 的真实身份与时长、服务端实际处理延迟、以及延迟归属（网络 / 客户端 / 服务端）三类证据，目前**一项都没有**。
- **服务端（可能）**：若服务端对这类输入不返回或不及时返回，本仓改时限只是把失败推后。
- **SDK（不能排除）**：指纹一致只排除版本漂移；SDK 的帧构造、`is_final` 语义、预算算法都没有被审计过，不能写「SDK 无证据」。
- **不提出**：通用兼容层、fallback 引擎、重试/超时堆叠——锁定决策已排除，且无第二个消费者。

## 八、锁定决策与方案状态

锁定（本卡遵守）：仅诊断；保留根因未知；不得把 FunASR 成功当 CapsWriter 修复；不得从 121 秒直接认定时限过短；不得从 5.55s < `file_seg_duration=25` 推定分段缺陷。

方案状态（区分「已否决」与「未证实」很重要）：

| 说法 | 状态 | 理由 |
| --- | --- | --- |
| 「调大自动预算 / 传更大的 `deadline_total` 就修好了」 | **已否决** | 只改失败时刻；且现有测试 `test_unknown_duration_omits_deadline_kwarg` 把「拿不到时长就不传」锁成有意决策 |
| 「换 FunASR 引擎绕过」 | **已否决** | 锁定决策排除，属掩盖失败 |
| 「重开 #155 套旧修法」 | **已否决** | #155 修的是长媒体按实测吞吐算预算，前提是拿得到时长；本例前提不成立 |
| 「上游已有同因修复，直接升级」 | **未证实** | 上游在办 PR/issue（#52/#53/#59/#60、open 的 #61~#64、#43/#55/#56/#61）均未出现「短音频客户端预算/短尾分段」条目；「没有相关条目」不等于「已排除同类缺陷」 |
| 「FunASR 成功 = CapsWriter 已修」 | **已否决** | 另一条链路，不构成 CapsWriter 的对照成功 |
| 「时限太短」 | **未证实** | 见第三节 |
| 「短音频分段缺陷」 | **未证实** | 见第三节 |

## 九、上游与本仓的只读复核

| 来源 | 观察 |
| --- | --- |
| 上游 issue #52（OPEN）音轨限定导致长文件 120s 超时 | 对应修法 PR #53 已合入 pin；样本 A 是纯 wav，不涉视频轨 |
| 上游 issue #43 / #55 / #56 / #61（OPEN） | 分别是容器时长估算、测试环境红、跨 ffmpeg 版本验证、HTTP 计费边界 |
| 上游 open PR #61~#64 | 均在 HTTP 容量三闸、QA、baseline 采集线上 |
| 本仓主干 `ff92a175` | 无相关提交；`tests/unit/test_capswriter_deadline_budget.py` 锁死「拿不到时长就不传 `deadline_total`」 |
| 本仓 `GenericDownloader.download_file` | 该方法内无 `_validate_media_file` 调用（源码检查 + 真实执行双证）。**注意**：这只说明这条路径不写 `last_media_duration`；本仓其他路径（如共享音轨准入 `_ensure_audio_track`）是否探测过时长、以及探测结果去了哪里，本卡未核实 |

## 十、命令与退出码（可复核）

生产只读（n305）：

| 命令 | 退出码 | 用途 |
| --- | --- | --- |
| `ssh n305 'docker ps --format … \| grep -i video; ls data'` | 0 | 容器 running/healthy、日志目录 |
| `ssh n305 'grep -n "\"server_url\"" config/config.jsonc'` | 0 | ASR 端点（第 18 行 = capswriter，第 165 行 = funasr） |
| `ssh n305 'grep -c "task_cc2eeeeb…" logs/app.log'` | 0 | 30 行命中 |
| `ssh n305 'grep -E "开始转录文件\|转录完成，生成文件\|转录文件失败" logs/app.log'` | 0 | 转录时间线白名单 |
| `ssh n305 'grep -c "transcription_deadline" logs/app.log'` | 0 | 9 行，8 行 `duration=unknown` |
| `ssh n305 'curl -s --max-time 8 http://<ASR_HOST>:6016/health \| python3 -c …'` | 0 | 只投影 4 个白名单字段 |
| `ssh n305 'docker exec … sha256sum …/capswriter_asr/client.py'` | 0 | 与 pin 比对（版本一致性） |

本地（worktree）：

| 命令 | 退出码 | 用途 |
| --- | --- | --- |
| `uv sync --offline` | 0 | 建 `.venv` |
| `PYTHONPATH=src uv run --no-sync python <probe_producer.py>` | 0 | producer 实参 + `last_media_duration` 缺失（附录 A.1/B.1） |
| `PYTHONPATH=src uv run --no-sync python <probe_sdk_deadline.py>` | 0 | 120.2s / 142.2s 放弃行为（附录 A.2/B.2），一次约 262 秒墙钟 |

上游只读：`gh issue view 166`、`gh api repos/zlxlabs/CapsWriter-ASR-Server/commits|pr list|issue list`（仅读，未评论未改）。

## 十一、继承红与新红

卡面写明主干基线 API 不可用（`gh api request failed`），因此**继承红未能判定**。本卡无代码改动、未跑全量测试（`make test` 未执行），因此不存在新红判定依据。

## 十二、踩到的坑

1. 卡面要求先读的两个技能文件 `~/.pi/agent/skills/deploy-ops/SKILL.md` 与 `~/.pi/agent/skills/investigate/SKILL.md` 均返回 `Permission denied`，**未能读取**；本卡按全局约定自行执行同口径纪律。此为卡面执行前置条件失败，已如实报告。
2. 探针初版用 `aiohttp` 起假服务端，dev 依赖里没有（aiohttp 在 perf extra）→ `ModuleNotFoundError`；改用已安装的 `websockets 15` 的 `process_request` 钩子同端口兼做 `/health`。
3. `transcribe_file_sync` 内部是 `asyncio.run()`，在已有 loop 里调用直接 `RuntimeError`，且同时把 coroutine 泄漏成 never-awaited 警告——只看该警告会误判超时逻辑有问题。改用等价 async 入口 `transcribe_file`。
4. `websockets.http11.Response` 的 headers 必须传 `Headers` 对象，传 `dict` 会在握手时抛 `AttributeError: 'dict' object has no attribute 'serialize'`。
5. 探针 A 初版 mock 了 `requests.get`，而 `GenericDownloader` 实际走 `_safe_request`/`_dispatch_pinned_request` 钉 IP 路径，**真的解析 DNS 并向合成域名 example.com 发了 3 次请求**（全部 404）。改为 mock `_safe_request` 后不再出网。这是本卡唯一的意外外网访问，无凭据、无生产数据。
6. `GenericDownloader.__init__` 需要 `config/config.jsonc`（本 worktree 无此文件，且本卡不改配置），改用 `__new__` 绕过并按基类初值补 `last_media_duration=None`。
7. 公开仓 pre-push 扫描拦截内网 IP，报告内 ASR 端点改为 `<ASR_HOST>:6016` 占位符后才推送成功。
8. 上一版把「同期任务处理耗时」当成预算证据、把媒体标题写进报告、把墙钟估成 50 分钟——三处都是本卡自己的推断错误，不是环境问题；本版已逐条纠正，历史提交不改写（见第十四节）。

## 十三、闸与绕过

- 只写本文件与 `progress/caps-short-progress.md`；未改业务代码、依赖、config、CI。
- 生产侧只有：容器/日志只读 grep、固定 task id 过滤、单个 `/health` GET、容器内文件 sha256。未 POST 新任务、未向 ASR 提交音频、未重启/部署、未操作 issue、未读取 users.json 或任何凭据（`server_url` 是主机名+端口，非凭据）。
- 日志只投影白名单字段（时间、级别、模块行号、错误码、耗时），task id 与路径前缀截断；媒体标题/文件名已从本版移除。
- 绕过说明：为不读 config 而用 `__new__` 构造 downloader；为在已有 loop 内可用而用 SDK 的 async 入口替代 `transcribe_file_sync`（语义等价，仅少一层 `asyncio.run` 包装）。
- 预算：定向检索约 20 次、n305 SSH 8 次（卡面限 12），单条均 ≤30s。墙钟以实际执行起止计：派发 `02:36Z` 至最后一次 push `≈03:00Z`，约 24 分钟，**在 40 分钟软限内**（上一版「约 50 分钟、超软限」的说法不成立，已撤回）。

## 十四、与卡面的偏差

- 卡面要求「比较历史任务运行版本」：历史任务运行的就是基线本身，无版本差；改为核对容器内实际安装 SDK 文件的 sha256，且只把它当作版本一致性证据。
- 卡面禁止向生产 ASR 提交音频，故缺失的服务端侧实验未执行；本版进一步明确：网络层与服务端层的观测同样是缺口，不能只用客户端复现代替。
- 输入样本复核为「低」：原文件已清理，无法取 hash；且 5.55s 本身未经服务端记录确认。
- **历史提交含非必要信息（不改写，供审计）**：`66d00751`/`86e9d55a` 的正文里出现过同期任务的媒体标题与临时文件名、以及已被撤回的强推断和错误的墙钟估计。修订未授权改写历史，故以本节与本版正文为准，旧提交可从 GitHub 历史中查到。

## 十五、最贵的一步

把「121 秒」从数字变成一条可复核的客户端机制链：生产两条日志（`duration=unknown` + `code=timeout` 原文）→ 代码路径逐跳核对 → 本地 synthetic 探针把 120.2 秒与逐字文案复现，并额外拿到「显式 5.55s 只有 142.2 秒」这个反向刻度。
**这一步的代价是它容易诱导过度归因**：机制链闭合到客户端为止，服务端与网络两段没有任何观测；本版的修订正是把这条闭合链的边界重新画回事实。

---

## 附录 A：探针源码（原样迁入，未改写）

### A.1 `probe_producer.py`

```python
"""Issue #166 本地无网络探针：观察本仓真实传给 SDK 的参数（producer 侧）。

不做生产复现，不连任何 ASR；只捕获 CapsWriterClient.transcribe_file 发给
capswriter_asr.transcribe_file_sync 的 kwargs，以及 GenericDownloader 真实
download_file 路径是否写 last_media_duration。
"""

import json
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

from capswriter_asr import Transcript

from video_transcript_api.downloaders.generic import GenericDownloader
from video_transcript_api.transcriber.capswriter_client import (
    CapsWriterClient,
    Config,
)

OUT = {}


def _make_client(output_dir: Path) -> CapsWriterClient:
    with patch.object(CapsWriterClient, "__init__", lambda self: None):
        client = CapsWriterClient()
    client.output_dir = str(output_dir)
    client.max_retries = 1
    client.retry_delay = 0
    client.log = MagicMock()
    return client


def _fake_transcript():
    raw = {"task_id": "task-probe", "time_start": 1.0, "time_complete": 2.0, "text_accu": "ok."}
    return Transcript(
        text="ok.",
        tokens=list("ok."),
        timestamps=[0.0, 0.5, 1.0],
        duration=5.55,
        raw=raw,
    )


def probe_sdk_kwargs(audio: Path, tmp: Path, media_duration):
    seen = {}

    def _capture(path, url, **kwargs):
        seen["path"] = str(path)
        seen["url"] = url
        seen["kwargs"] = dict(kwargs)
        return _fake_transcript()

    Config.generate_funasr_compat = False
    Config.generate_txt = True
    Config.generate_json = False
    Config.generate_merge_txt = False
    Config.server_addr = "probe-host"
    Config.server_port = 6016
    with patch(
        "video_transcript_api.transcriber.capswriter_client.transcribe_file_sync",
        side_effect=_capture,
    ):
        ok, _ = _make_client(tmp).transcribe_file(str(audio), media_duration=media_duration)
    OUT[f"sdk_kwargs_media_duration={media_duration!r}"] = {
        "success": ok,
        "url": seen["url"],
        "kwargs": seen["kwargs"],
    }


def probe_generic_duration(tmp: Path):
    """真实调用 GenericDownloader.download_file（只 mock HTTP 响应，不发网络请求）。"""
    # 用 __new__ 绕过 __init__：避免读 config/config.jsonc（本卡不改配置）。
    # last_media_duration / config 用等价初值补齐（基类 __init__ 就是这个初值）。
    dl = GenericDownloader.__new__(GenericDownloader)
    dl.last_media_duration = None
    dl.config = MagicMock()
    dl.temp_manager = MagicMock()
    dl.temp_manager.get_current_task_dir.return_value = str(tmp / "taskdir")
    payload = b"RIFF" + b"\0" * 64
    resp = MagicMock(status_code=200, headers={"Content-Length": str(len(payload))})
    resp.iter_content = MagicMock(return_value=iter([payload]))
    resp.__enter__ = lambda s: s
    resp.__exit__ = lambda s, *a: False
    # 只 mock SSRF 校验后的出网点 _safe_request：否则会真的解析 DNS 并请求外网
    with patch.object(GenericDownloader, "_safe_request", return_value=resp):
        path = dl.download_file("https://example.com/probe.wav", "probe.wav")
    OUT["generic_download"] = {
        "returned_path_is_str": isinstance(path, str),
        "last_media_duration": repr(dl.last_media_duration),
        "calls_base_probe": "_validate_media_file" in (
            subprocess.run(
                [sys.executable, "-c", "import inspect;from video_transcript_api.downloaders.generic import GenericDownloader as G;print('_validate_media_file' in inspect.getsource(G.download_file))"],
                capture_output=True, text=True,
            ).stdout
        ),
    }


def main():
    tmp = Path(tempfile.mkdtemp(prefix="caps-short-probe-"))
    audio = tmp / "probe-5.55s.wav"
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
         "-i", "sine=frequency=440:duration=5.55", "-ar", "16000", "-ac", "1", str(audio)],
        check=True,
    )
    probe_real = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "json", str(audio)], capture_output=True, text=True
    )
    OUT["fixture"] = {
        "path": str(audio),
        "bytes": audio.stat().st_size,
        "sha256": subprocess.run(["sha256sum", str(audio)], capture_output=True, text=True).stdout.split()[0],
        "ffprobe_duration": json.loads(probe_real.stdout)["format"]["duration"],
    }
    for dur in (None, 0.0, 5.55, 93.08898):
        probe_sdk_kwargs(audio, tmp, dur)
    probe_generic_duration(tmp)
    print(json.dumps(OUT, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
```

### A.2 `probe_sdk_deadline.py`

```python
"""Issue #166 本地无网络探针（SDK 侧真实观测；transcribe_file_sync 在已有 loop 内不可用，改用等价的 async 入口）：自动预算到底等多久、报什么。

同一端口上用 websockets 15 的 process_request 钩子同时提供：
  - GET /health -> HTTP 200 JSON（protocol_version=2, encodings=["flac"]）
  - 其余路径升级为 WS，接收音频帧后**永不回应**（模拟服务端收下任务却不返回终态）

用真实 SDK 的 transcribe_file_sync 跑两次：
  1. 不传 deadline_total（自动预算）——输入是 5.55s wav，理论预算 max(120, 5.55+60)=120
  2. 显式 deadline_total=142.2（本仓公式 5.55*4+120）——观测失败时刻是否后移

不连任何真实 ASR，不碰生产。
"""

import asyncio
import json
import subprocess
import tempfile
import time
from pathlib import Path

from websockets.asyncio.server import serve
from websockets.datastructures import Headers
from websockets.http11 import Response

from capswriter_asr import AsrError, transcribe_file

HOST, PORT = "127.0.0.1", 8791
RECEIVED = []


def process_request(connection, request):
    if request.path == "/health":
        body = json.dumps(
            {"protocol_version": 2, "encodings": ["flac"], "role": "server"}
        ).encode()
        return Response(200, "OK", Headers([("Content-Type", "application/json")]), body)
    return None


async def handler(ws):
    async for message in ws:
        frame = json.loads(message)
        RECEIVED.append(
            {
                "source": frame.get("source"),
                "seg_duration": frame.get("seg_duration"),
                "seg_overlap": frame.get("seg_overlap"),
                "encoding": frame.get("encoding"),
                "is_final": frame.get("is_final"),
                "samples_total": frame.get("samples_total"),
                "b64_bytes": len(frame.get("data") or ""),
                "keys": sorted(frame.keys()),
            }
        )
    # 故意不回应任何 result 帧：模拟服务端挂住


async def main():
    tmp = Path(tempfile.mkdtemp(prefix="caps-short-sdk-"))
    audio = tmp / "probe-5.55s.wav"
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
         "-i", "sine=frequency=440:duration=5.55", "-ar", "16000", "-ac", "1", str(audio)],
        check=True,
    )
    dur = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", str(audio)],
        capture_output=True, text=True,
    ).stdout

    async with serve(handler, HOST, PORT, process_request=process_request, ping_interval=None):
        url = f"ws://{HOST}:{PORT}"
        results = []
        for label, deadline in (("auto_budget_no_deadline_total", None), ("deadline_total_142.2", 142.2)):
            kwargs = {"encoding": "flac", "seg_duration": 25, "seg_overlap": 2}
            if deadline is not None:
                kwargs["deadline_total"] = deadline
            started = time.monotonic()
            try:
                transcript = await transcribe_file(str(audio), url, **kwargs)
                results.append({"label": label, "elapsed": round(time.monotonic() - started, 1),
                                "outcome": "success", "duration": transcript.duration})
            except AsrError as exc:
                results.append({"label": label, "elapsed": round(time.monotonic() - started, 1),
                                "outcome": "asr_error", "code": exc.code,
                                "message": exc.message, "retryable": exc.retryable})
            except Exception as exc:  # noqa: BLE001
                results.append({"label": label, "elapsed": round(time.monotonic() - started, 1),
                                "outcome": type(exc).__name__, "message": str(exc)[:200]})

    print(json.dumps(
        {"fixture": {"ffprobe": json.loads(dur)["format"]["duration"],
                     "bytes": audio.stat().st_size,
                     "sha256": subprocess.run(["sha256sum", str(audio)], capture_output=True, text=True).stdout.split()[0]},
         "results": results, "frames_received": RECEIVED},
        ensure_ascii=False, indent=2,
    ))


if __name__ == "__main__":
    asyncio.run(main())
```

## 附录 B：探针真实输出（原样，未编辑）

### B.1 `probe_producer.py` → `producer_out.json`

> 说明：原始运行只把 JSON 打到终端、未落盘；修订时按**同一脚本**重跑一次（秒级、本地、无网络请求）并落盘。数值与原次一致（fixture sha256 相同）。不重跑 262 秒的 SDK 探针。

```json
{
  "fixture": {
    "path": "/tmp/caps-short-probe-_6_w46k6/probe-5.55s.wav",
    "bytes": 177678,
    "sha256": "c08fd13b7d5eace2614478a1e03692d8f2bfee72c1390715a535caa0ebf6aca7",
    "ffprobe_duration": "5.550000"
  },
  "sdk_kwargs_media_duration=None": {
    "success": true,
    "url": "ws://probe-host:6016",
    "kwargs": { "encoding": "flac", "seg_duration": 25, "seg_overlap": 2 }
  },
  "sdk_kwargs_media_duration=0.0": {
    "success": true,
    "url": "ws://probe-host:6016",
    "kwargs": { "encoding": "flac", "seg_duration": 25, "seg_overlap": 2, "deadline_total": 120.0 }
  },
  "sdk_kwargs_media_duration=5.55": {
    "success": true,
    "url": "ws://probe-host:6016",
    "kwargs": { "encoding": "flac", "seg_duration": 25, "seg_overlap": 2, "deadline_total": 142.2 }
  },
  "sdk_kwargs_media_duration=93.08898": {
    "success": true,
    "url": "ws://probe-host:6016",
    "kwargs": { "encoding": "flac", "seg_duration": 25, "seg_overlap": 2, "deadline_total": 492.35592 }
  },
  "generic_download": {
    "returned_path_is_str": true,
    "last_media_duration": "None",
    "calls_base_probe": false
  }
}
```

同次运行的 stderr（日志行，只含合成路径与合成域名）：

```
11:02:41 | WARNING | capswriter_client:_transcription_deadline:141 - transcription_deadline duration=unknown fallback=sdk_auto
11:02:41 | INFO    | capswriter_client:_transcription_deadline:144 - transcription_deadline duration=0.00 value=120.0
11:02:41 | INFO    | capswriter_client:_transcription_deadline:144 - transcription_deadline duration=5.55 value=142.2
11:02:41 | INFO    | capswriter_client:_transcription_deadline:144 - transcription_deadline duration=93.09 value=492.4
11:02:41 | INFO    | downloaders.generic:download_file:628 - 开始下载文件 (尝试 1/3): https://example.com/probe.wav
11:02:41 | INFO    | downloaders.generic:download_file:717 - 文件下载成功: /tmp/caps-short-probe-<random>/taskdir/probe.wav (大小: 0.00 MB)
```

### B.2 `probe_sdk_deadline.py` → `sdk_probe_out.json`（原始那次，约 262 秒）

```json
{
  "fixture": { "ffprobe": "5.550000", "bytes": 177678, "sha256": "c08fd13b7d5eace2614478a1e03692d8f2bfee72c1390715a535caa0ebf6aca7" },
  "results": [
    { "label": "auto_budget_no_deadline_total", "elapsed": 120.2, "outcome": "asr_error",
      "code": "timeout", "message": "转录超过自动预算：远端转录阶段超时", "retryable": false },
    { "label": "deadline_total_142.2", "elapsed": 142.2, "outcome": "asr_error",
      "code": "timeout", "message": "转录超过deadline_total：远端转录阶段超时", "retryable": false }
  ],
  "frames_received": [
    { "source": "file", "seg_duration": 25, "seg_overlap": 2, "encoding": "flac",
      "is_final": true, "samples_total": 88800, "b64_bytes": 56540,
      "keys": ["data","encoding","is_final","samples_total","seg_duration","seg_overlap","source","task_id","time_start"] },
    { "source": "file", "seg_duration": 25, "seg_overlap": 2, "encoding": "flac",
      "is_final": true, "samples_total": 88800, "b64_bytes": 56540,
      "keys": ["data","encoding","is_final","samples_total","seg_duration","seg_overlap","source","task_id","time_start"] }
  ]
}
```

`frames_received` 是**本地假服务端**收到的两帧（两次运行各一帧），只说明 SDK 发送侧的形状；它**不**说明生产服务端收到了什么，也不排除服务端侧的短尾/分段处理问题。
