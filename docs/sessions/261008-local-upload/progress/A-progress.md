# A 卡进度

## 当前阶段

implementing；store/Resolver/cleanup 代码已提交为 `132daf96`，窄测五连绿、指定回归与 `make test` 均通过；blank/source/cleanup 三项变异均由 AssertionError 命中且恢复后绿。待设计/六里程碑文件提交、push 与 PR 独立审查。HTTP接收、worker、网页、生产额度和恢复演练尚未实现，不能启用生产上传。

## 本段结论（≤3句）

- A 使用现有 `CacheManager` 作为 store，新增 `local_uploads`；不增加 Service 转发层或账户/队列平台。
- Resolver 通过独立 `upload_` token 读取，旧 NOT NULL token 列保持空字符串；禁用、撤销、到期拒读且不改变有效成果清理保护。
- 最终窄测连续五次通过，指定 URL/history/cache/task/媒体锁清理回归与 `make test` 全量 gate 均通过；blank lookup、上传 source fallback、active-cache cleanup predicate 的移除各自触发普通 AssertionError，恢复后对应测试各绿。历史 `tests/cache/test_cache_cleanup.py` 会删除仓库 fixture，已改成 `tmp_path` 且消费 producer 返回的路径。

## 关键决策与否决方案

- 24 小时 key 窗口、5 分钟未来时钟偏差；重复 owner/key 通过 `BEGIN IMMEDIATE` 复用既有记录，metadata 指纹不同直接拒绝。
- 30 天自 root 首个 success/failed 的 `completed_at` 固定起算；never 仅以显式选择无到期，撤销通过 SQLite trigger write-once。
- 不将 `VTA_UPLOADS_ENABLED` 放进数据卷 JSON；生产额度、systemd恢复域未验证前保持关闭。
- 上传 reprocess 暂不接线，拒绝走 task_status owner legacy fallback；原媒体仍走 TempFileManager 临时清理。

## 下一步唯一动作

提交设计合同、六个里程碑和更新后的本进度记录，再推送两笔提交并创建 draft PR。
