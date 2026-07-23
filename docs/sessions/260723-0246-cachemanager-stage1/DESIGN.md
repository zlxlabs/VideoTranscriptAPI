# T2 物理位置与依赖方向

## 裁决

Stage 1 新增两个 API 层服务模块：

- `src/video_transcript_api/api/services/view_token_resolver.py`
- `src/video_transcript_api/api/services/task_dedup.py`

不新建 `application/` 层，也不改连接池、锁、schema 或缓存存储边界。项目已有 `api/services/`（如 `llm_ops.py`、`transcription.py`），而本阶段的两块逻辑恰好由 API 读取/提交流程消费；在该目录落位能减少 CacheManager 的业务职责，同时避免为个人项目引入额外的抽象层与装配成本。

`ViewTokenResolver` 负责以既有 CacheManager 提供的任务、缓存和 LLM 配置访问能力，组合 view token 对应的展示数据；其内部保留完整的诚实摘要状态模型。`TaskDedup` 负责按 URL 与 `(platform, media_id)` 复用任务的业务选择，沿用已有的三段状态排序规则。二者都是协调服务，不拥有 SQLite 连接、文件路径、锁或表结构。

## 依赖与装配

路由层在现有 `cache_manager` 单例旁构造服务，并显式注入该实例：`ViewTokenResolver(cache_manager)`、`TaskDedup(cache_manager)`。这确保所有操作复用当前 CacheManager 的连接池/`_get_cursor` 生命周期和既有审计行为，不创建第二个连接池或 application 层对象图。

为保持向后兼容，`CacheManager` 保留六个原方法作为薄 facade，分别委托给已注入或惰性创建的服务；原调用方可在 T5 逐步改用服务，但外部/测试调用不会断裂。服务方法应接收与现有方法相同的参数并维持 `Optional[dict]`、`None` 和日志语义，避免把异常处理从当前边界挪到路由层。

## 循环导入规避

运行时依赖方向固定为：`api/routes` → `api/services` → CacheManager **实例能力**。服务模块不在运行时导入 `cache.cache_manager.CacheManager`；类型标注使用 `typing.TYPE_CHECKING` 下的前向引用，或定义最小 `Protocol` 描述所需方法。CacheManager facade 若需创建服务，必须在方法内局部导入，或由外部装配后注入，不能在 `cache_manager.py` 模块顶层反向 import `api.services`。这避免 `CacheManager → api.services → CacheManager` 的初始化环，同时仍使 facade 保持很薄。

排序常量仍由 CacheManager 唯一持有并通过注入实例读取；不在 `TaskDedup` 复制 SQL 文本。这样 `get_task_by_view_token` 与两个 dedup 查询继续共享同一状态优先级定义。
