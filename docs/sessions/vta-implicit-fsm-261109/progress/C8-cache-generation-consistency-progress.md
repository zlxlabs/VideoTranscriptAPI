## 里程碑 1：Base 红测

- 当前阶段：TDD 红测已完成，尚未实现
- 本段结论：真实 `save_cache` → `get_cache` 链路在第二个侧车替换失败后返回新正文配旧 segments；两次替换间并发读取也复现同一混代。两条新增测试在基线 `b54657b4dc422d5579735e13bbaf2332856496d9` 均失败。
- 关键决策与已否决方案：不得调换正文和侧车写入顺序；先以整组替换失败和并发读取实测确认问题，再按数据库行切换不可变 generation 的方向实现。
- 下一步唯一动作：提交红测里程碑，并实现 generation 发布与数据库行提交点。

## 里程碑 2：实现不可变 generation 发布

- 当前阶段：实现完成，进入提交后窄测
- 本段结论：`save_cache` 将新产物组写入新的不可变 generation 目录，数据库行的 `files_loc` 更新作为唯一发布点；写入失败时旧数据库行仍指向完整旧组。旧版确定性目录仍可由 `get_cache` 读取；重写时从当前变体（缺席时 sibling）的目录复制既有其他产物。
- 关键决策与已否决方案：没有调整正文与侧车的替换先后；旧 generation 不在发布后立即删除，避免尚在读取旧 `files_loc` 的读者遇到已删除目录。
- 下一步唯一动作：提交实现、配套回归与本段进度，然后运行完整 `tests/cache/` 窄测。

## 里程碑 3：本地验收与范围闸

- 当前阶段：本地窄测通过，等待推送与 draft PR
- 本段结论：`uv run --frozen pytest -q tests/cache/` 通过（61 项），Accept-Check 文件通过（2 项）；`card_scope_check.py` 确认基线至 HEAD 的 4 个变更文件均在授权范围内。按 `ci-standard-v1`，本机未运行远程 `make test`。
- 关键决策与已否决方案：旧 generation 不立即删除，避免数据库指针切换前已取得旧路径的读者失去文件；报告申明 generation 回收不在本卡范围内。
- 下一步唯一动作：推送当前分支并创建正文引用卡面标题的 draft PR。

## 里程碑 4：消费方核查与 generation 回收

- 当前阶段：按已批准范围补齐存储布局兼容测试，准备更新 PR
- 本段结论：窄测发现两项 unit 测试仍硬编码旧目录布局；scope seq=1 批准 `tests/unit/test_cache_manager.py` 断言 generation 目录，seq=2 批准 `tests/unit/test_cache_timeline_wiring.py` 验证替换后旧 generation 删除和当前 generation 无侧车。实现现已在数据库提交后、同一 `media_lock` 内删除不再被任何数据库行引用的旧 generation；`get_cache` 也在同一锁内刷新行并读取，避免读者跨过回收窗口。
- 关键决策与已否决方案：scope seq=1 批准原文：必须全仓 grep 绕过 files_loc 的读写方并在报告逐行说明；若有卡外必改消费方须再申请。scope seq=2 批准原文：旧 generation 不得永久保留；数据库切换后在同一 media_lock 内删除未再引用的旧 generation，旧路径消失时读者可明确不可用。
- 下一步唯一动作：提交本段兼容修复与报告更新，运行最终范围闸并推送一次。

## 里程碑 5：消费方回归通过

- 当前阶段：代码与兼容测试定稿，待提交
- 本段结论：`tests/cache/` 61 项、相关缓存/时间线/持久化 unit 127 项、files_loc 消费方相关 unit 105 项及 Accept-Check 2 项均通过。全仓 Python 源码 grep 未发现卡外消费方按旧 deterministic root 读取 transcript；所有 transcript 消费方使用数据库 `files_loc` 或 `get_cache` 返回的 `file_path`。
- 关键决策与已否决方案：批准范围内更新两份 unit 断言，不强求旧 generation 永久留盘；保存成功后只删除未被任意 `video_cache` 行引用的 generation，legacy root 不整体删除。`api/services/summary_ratio_stats.py`、`api/services/metadata_editor.py` 与 views 使用 `files_loc`；`llm/core/cache_manager.py` 的 key-info/legacy mapping 是独立根目录缓存，运行期 SpeakerInferencer 使用 `api/context.py` 注入的 CacheManager。
- 下一步唯一动作：提交当前修改与本段进度。

## 里程碑 6：generation 生命周期与上传消费者

- 当前阶段：最终实现与授权测试已通过，待提交
- 本段结论：scope seq=3 批准只改 `tests/integration/test_local_upload_lifecycle.py` 中两个过时的 `_get_file_path` 断言，改为经 `get_cache` 的 `file_path` 验证；local_uploads 生产函数未改。当前 `tests/cache/` 62 项、相关缓存 unit 127 项、files_loc 消费方 unit 105 项、两组 local-upload 集成场景 4 项及 Accept-Check 2 项均通过。
- 关键决策与已否决方案：generation 清理只按数据库引用判断；变体共享旧 `files_loc` 时保留该目录。无引用时 cleanup 删除 generation 子目录并回收空 `.generations` 父目录；新保存遇到遗留无引用 generation 时清掉它们再发布。scope seq=3 批准原文：只改测试定位；报告逐条说明双变体引用、`save_speaker_mapping`/`save_llm_status`/`_save_llm_results` 目录可见性、cleanup 与孤儿目录回收。
- 下一步唯一动作：提交本段代码、授权范围内测试和进度记录。

## 里程碑 7：范围内测试保持最小

- 当前阶段：最终目标测试通过，准备提交
- 本段结论：在补齐 base media 目录存在及旧 generation 回收后，`tests/unit/test_cache_manager.py` 与 `tests/unit/test_cache_timeline_wiring.py` 原断言恢复通过，因此不再修改这两份 unit 文件；scope seq=1/2 的批准范围未实际纳入最终 diff。`tests/integration/test_local_upload_lifecycle.py` 仍需要按数据库 `files_loc` 读取，已按 seq=3 获批改为经 `get_cache` 定位。
- 关键决策与已否决方案：不保留会因读者不认识 generation 布局而失效的根目录测试改动；不触碰 local_uploads 生产函数。最终复验 `tests/cache/` 62 项、缓存/时间线/持久化 unit 127 项、files_loc 消费方 unit 105 项、local-upload 集成 4 项及 Accept-Check 2 项均通过。
- 下一步唯一动作：提交当前实现、测试与进度记录。
