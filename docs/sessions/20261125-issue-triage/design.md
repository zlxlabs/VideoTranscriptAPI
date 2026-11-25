# 2026-11-25 缓存与 LLM 状态分诊设计

## 分诊结论与四个边界

本次将耦合在同一缓存目录上的问题拆成四个验收边界，避免把读侧兼容、写侧隔离和状态策略混成一个改动：

1. **#328-A（本卡）— 变体写隔离与 generation 绑定**：读缓存仍允许 `use_speaker_recognition=False` 回退命中 `True`；写侧按 `(platform, media_id, bool(use_speaker_recognition))` 精确选行。新变体不得复制兄弟变体目录；异步 handoff 携带 `files_loc` 后，提交时必须在媒体锁内验证它仍是当前代。
2. **#328-B（后续单独验收）— 结果页/重处理入口绑定**：结果页 token 解析及其重处理入口应绑定具体缓存记录，而不是在本卡改动；本卡不触碰 resolver、views 路由或该入口。
3. **#327（后续单独验收）— `notes_status` 状态合并**：状态合并策略独立于目录选择；本卡只保证写到正确变体与代次，不改 `notes_status` 的合并规则。
4. **跨层共享契约**：转录缓存发布、LLM 分层读取/写入、状态文件撤销/提交共同使用媒体锁；SQLite 行的 `files_loc` 是目录代次的发布指针。跨层测试须从 SQLite 读取真实两行并核查磁盘原始字节，不能以回退式 `get_cache(False)` 代替验证 False 行。

上述边界让 #328-A 可独立修复目录污染，同时保留 #328-B 与 #327 各自的根因、评审和验收闭环。

## 不变式

- `get_cache` 的缺省读语义不变：请求 `False` 时允许复用 `True`；请求 `True` 或传入整数 `1` 时过滤到真值变体。
- 写目标总是精确匹配布尔化变体。所有路径定位从精确 SQLite 行读取 `files_loc`，不通过排序回退选目录。
- `expected_files_loc` 是转录/缓存命中 handoff 的目录快照。写侧在 `media_lock(platform, media_id)` 内重新读取精确行；目录不同则报 `CACHE-GENERATION-MISMATCH` 并中止写入。
- `save_cache` 只从同变体行复制旧目录，以保留同变体重存的 LLM 产物；第一次创建兄弟变体使用空 generation 目录。
- LLM 状态撤销、产物原子写、状态提交仍处于现有媒体锁临界区；不引入跨文件回滚。

## 共享资源清单

| 资源 | 写入方 | 读取方 | 守卫 | 文件:行 |
| --- | --- | --- | --- | --- |
| `media_lock(platform, media_id)` 同媒体互斥锁 | `save_cache`；`invalidate_llm_status`；`save_llm_result`；`save_llm_status`；`llm_ops._save_llm_results` 与 notes 提交段 | `get_cache` 读取目录内产物；`llm_ops` 对同一媒体做层存在判定及提交 | 每媒体一把可重入锁；精确 variant 查询；带 `expected_files_loc` 时同锁核代 | `src/video_transcript_api/cache/cache_manager.py:288`、`1181`、`1385`、`1744`、`1861`、`1939`；`src/video_transcript_api/api/services/llm_ops.py:501`、`2135` |
| `llm_status.json` 状态文件 | `save_llm_status`；`invalidate_llm_status` 删除旧文件后由后续提交重建 | `get_cache` 读取状态；`invalidate_llm_status` 读取撤销前快照；`llm_ops` 读取合并后的状态 | 同媒体锁；精确变体目录；可选 `expected_files_loc` generation 核验；先撤销后写产物、最后提交状态 | `src/video_transcript_api/cache/cache_manager.py:1744`、`1939`、`1385`；`src/video_transcript_api/api/services/llm_ops.py:2269`、`2671` |

## 本卡实施范围与明确排除

本卡修改 `cache_manager.py` 的写目标选择和 sibling generation 建立规则；以 `variant_isolation.py` 集中精确行/目录及 generation mismatch 逻辑；`transcription.py` 在缓存命中或 `save_cache` 返回后把实际 `files_loc` 交给 LLM 阶段；`llm_ops.py` 对章节输入、层存在判定、分享卡落盘、产物哈希和状态回读指定精确变体，并把 generation 快照传给所有写操作。数据库 schema 不变。

`ViewTokenResolver`、views 重处理入口和 `notes_status` 状态合并策略均留在各自后续任务中，不在本卡借机改动。