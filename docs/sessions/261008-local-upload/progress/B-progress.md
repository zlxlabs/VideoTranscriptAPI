# B 卡进度

## 当前阶段

implementing；完成第一份独立可运行单元：bounded metadata decoder 与Unicode/字段约束测试通过。尚未实现 HTTP 接收、队列/SQLite 原子交接、worker readiness、实际integration验证。

## 本段结论（≤3句）

- metadata 使用有界 UTF-8 base64url JSON；显示文件名规范化为 basename，不作为路径；处理选项沿用既有 normalize_processing_options。
- `tests/unit/test_upload_routes.py` 通过，初始失败为值差异 AssertionError，修复后全绿。
- 尚无验证失败归因或生产环境结论。

## 关键决策与否决方案

- 不消费客户端文件hash；元数据只声明正字节数，正文由服务端流式计数/hash。
- source_url 只接受无userinfo的 http/https 文本，不抓取。

## 下一步唯一动作

扩展 CacheManager 持久 admission/root/media mapping 与 runtime 限额/预留，再接上真正 HTTP stream 和 dispatcher 测试。

## Milestone 2：流式HTTP与持久队列交接

### 本段结论（≤3句）

- 已实现 GET capabilities、raw `application/octet-stream` POST、owner receipt/DELETE、服务端流式 byte count/SHA-256、metadata边界、unique owner/key、24h截止与过期receiving清理。
- H0实现曾在SQLite root/accepted/mapping commit之后才调用队列 `put_nowait`；H1续补将同步 `put_nowait` 移入同一事务提交前，dispatcher仍在 PROCESSING/executor.submit 前核验持久受理与 root/media/path 映射，local_upload 走现有ASR路径且不经过URL downloader。
- 真实 ASGI+临时SQLite/dispatcher窄测：`tests/unit/test_upload_routes.py tests/unit/test_upload_dispatch.py tests/integration/test_upload_intake.py` 全绿（32 passed）；尚未跑全仓 make test、裸shell/systemd验证或 CI。

### 关键决策与否决方案

- upload limits 只从现有 `storage.upload_limits` 消费，示例四值均 null；`VTA_UPLOADS_ENABLED` 独立于数据卷配置，只有env、正有限额度及实际存活的 process_task_queue 同时满足才报告 enabled。
- body接收目录在开始读取前持久记录并由 TempFileManager active 集保护；未移交时由接收者清理，移交后worker清理；worker future完成释放原始输入的磁盘/inflight预留。
- `receiving` key在创建+24h截止前保留唯一；正式接受在截止点及以后显式410，过期receiving草稿和未移交暂存路径由maintenance退休；不自动重试/换key。
- 队列预满时在读body前拒绝；H0的事后撤回补偿仅属旧实现，H1已删除。QueueFull由事务回滚留在receiving/失败回执；commit失败时已入队的stale item由dispatcher durable gate丢弃。

## Milestone 3：全仓验证与交付

### 本段结论（≤3句）

- `make test` 完整门禁 exit 0；输出到 `/tmp/vta-upload-B-make-test.log`，pytest 到 100%，3 个既有用例 skip，无失败。
- 同一未设置 `VTA_UPLOADS_ENABLED` 的消费测试分别在 `env -i` 裸shell与临时 systemd user unit `vta-upload-B-env-verify-1791469452-1251284.service` 运行成功；未修改真实 unit/生产配置。
- commit `9ed74d9b5997aea4acbd3722b2cd6db537571faa` 已 push，draft PR #202：`https://github.com/zlxlabs/VideoTranscriptAPI/pull/202`；本地树clean。CI / 独立主审由lead托管，尚未ready/merge。

### 关键决策与未验证项

- 上传默认保持关闭，示例四项生产额度均为null；本地测试额度只存在临时 fixture，不作为生产容量结论。
- 真实生产 systemd unit、ASR服务容量、恢复域和部署仍未验证；本卡未触碰真实配置、数据、通知或生产ASR。

## Milestone H1：补齐 R3 与接收文件所有权顺序

### 当前阶段

implementing；H1最小顺序修复已提交并push至原PR分支，随后完成窄测、A/URL回归及全量make test。PR保持draft，CI/独立审查由lead托管。

### 本段结论（≤3句）

- H0真实HTTP/SQLite/queue handoff负对照观察到commit前状态已是accepted/root queued；H1在H0上四个候选producer/consumer顺序测试实际失败均为AssertionError，修复后四项通过。
- 当前H1将一次同步`put_nowait`从route移入`CacheManager.accept_local_upload`事务回调；commit失败会留下stale queue payload，由dispatcher在PROCESSING/submit/ASR前拒绝。事后cancel acceptance方法和callsite已删除。
- receiving cleanup现在先于unlink在`BEGIN IMMEDIATE`中重读、核验owner-key时间/仍receiving/路径未变并删除记录，commit后才unlink；两个controlled order测试分别锁定accepted文件保留和retire先赢。

### 关键决策与验证

- 新参数`enqueue`只服务HTTP队列跨SQLite原子发布边界；callback同步调用且无await，既有A store-level direct acceptance仍可不带queue callback。没有新增服务层/状态/锁/重试/fallback。
- 真实测试位于`tests/integration/test_upload_intake.py`：队列put hook用独立SQLite连接观察未提交receiving/root-none和实际payload；commit fault时实际queue收到payload，真实dispatcher drain后没有PROCESSING/executor submit/ASR；cleanup/accept两顺序均使用真实临时SQLite与文件字节，Pause仅位于测试fixture。
- H0红验命令：`uv run --frozen pytest -q tests/integration/test_upload_intake.py::test_http_queue_handoff_precedes_durable_acceptance_commit tests/integration/test_upload_intake.py::test_sql_commit_failure_leaves_stale_queue_item_for_dispatcher_to_drop tests/integration/test_upload_intake.py::test_receiving_cleanup_rechecks_after_accept_wins_and_preserves_owned_file tests/integration/test_upload_intake.py::test_receiving_cleanup_retires_before_unlink_and_accept_loses`；H0四项均以AssertionError显示期望receiving/null或文件存在、实际accepted/root或文件已删；H1同四项4 passed。
- H1窄测`uv run --frozen pytest -q tests/unit/test_upload_routes.py tests/unit/test_upload_dispatch.py tests/integration/test_upload_intake.py`：35个测试点通过；另含A store/Resolver、URL API/history、runtime及maintenance的回归命令退出0。
- H1代码提交`5b1f3526fa958deffd704ba43cbc13be47db95df`已push；四个H0红测在H1已绿，H1末尾窄测+A/URL回归退出0。
- 按红验安全要求，在代码已提交后分别临时把enqueue移到SQL commit之后、把unlink移到SQLite retirement之前；对应producer/consumer断言各自以AssertionError变红，恢复单个顺序后各自绿。变异输出记录在`/tmp/vta-upload-H1-r3-mutation.log`及`/tmp/vta-upload-H1-cleanup-mutation.log`。
- H1 `make test` 与 `uv run --frozen pytest -q -rs tests` 均exit0；3个skip均因`tests/unit/test_capswriter_samples_total_contract.py`缺少真实preflight fixture。完整结果记录在`/tmp/vta-upload-H1-dlg-20261008-151543-50422d-*`。

### 下一步唯一动作

push本进度证据并写终态报告；PR保持draft交lead做H0..H1增量review和CI，不ready/merge。
