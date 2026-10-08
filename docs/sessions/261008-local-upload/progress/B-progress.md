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
- SQLite root/accepted/mapping一次事务提交后只调用一次有界队列 `put_nowait`；worker 在 PROCESSING/executor.submit 前核验持久受理与 root/media/path 映射，local_upload 走现有 ASR 路径且不经过 URL downloader。
- 真实 ASGI+临时SQLite/dispatcher窄测：`tests/unit/test_upload_routes.py tests/unit/test_upload_dispatch.py tests/integration/test_upload_intake.py` 全绿（32 passed）；尚未跑全仓 make test、裸shell/systemd验证或 CI。

### 关键决策与否决方案

- upload limits 只从现有 `storage.upload_limits` 消费，示例四值均 null；`VTA_UPLOADS_ENABLED` 独立于数据卷配置，只有env、正有限额度及实际存活的 process_task_queue 同时满足才报告 enabled。
- body接收目录在开始读取前持久记录并由 TempFileManager active 集保护；未移交时由接收者清理，移交后worker清理；worker future完成释放原始输入的磁盘/inflight预留。
- `receiving` key在创建+24h截止前保留唯一；正式接受在截止点及以后显式410，过期receiving草稿和未移交暂存路径由maintenance退休；不自动重试/换key。
- 首次QueueFull在读body前拒绝；若数据库已提交后`put_nowait`仍失败，事务补偿撤回root/token映射、保留失败receipt并清理文件，不能返回202。

## 下一步唯一动作

完成文档/测试说明与全仓门禁，核对每个不变量的producer/consumer锁定测试，再更新本进度并提交推送下一里程碑。
