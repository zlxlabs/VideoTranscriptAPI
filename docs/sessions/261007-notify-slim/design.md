# 设计：精简企业微信 / 飞书任务通知（261007-notify-slim）

## 问题

一个普通转录任务（下载 + 语音识别 + 大模型总结，不走缓存）会向每个渠道发 7 条消息，企业微信和飞书都开着就是 14 条。
同时跑多个任务时，各任务的消息交错在一起，而且消息不带任务编号，"开始处理"那条连标题都没有，很难对应回具体任务。

## 原则

推送会打断人，只在三种情况下值得发：

1. 有事要人去做（失败、卡住）；
2. 等待的成果到了（总结出来了）；
3. 确认任务已被接收。

进度类消息（正在下载、正在转录、转录完成但后面还有步骤）三条都不满足，它们属于查看页上随时可查的状态，不该主动推送。
只要异常一定会通知，"没收到异常通知"本身就说明一切正常。

## 消息清单（改后）

| 消息 | 何时发 | 内容 |
|---|---|---|
| 📥 已接收 | 提交路由排队成功 | 抬头 + 链接 + 查看页 |
| ✅ 完整总结 + ✅ 分享回执 | 任务成功终态（所有成功路径统一；由同一条 outbox 按顺序提交两条） | 先完整完成正文（不在业务层截断），再短回执（统一抬头 + 原始地址 + view URL + 网页分享摘要首段） |
| ❌ 失败 | 任务失败终态 | 抬头 + 失败状态 + 错误原因 + 查看页 |
| ⏳ 偏慢 | 超过预估时间，每个任务最多一次（第 2 张卡） | 抬头 + 当前阶段 + 已用时长 / 预估时长 + 查看页 |

服务器语音识别服务宕机和恢复的告警（ASR 监控）不属于单个任务，保持不变。

**删除的消息**：开始处理、正在下载视频、下载进度、正在转录音视频、转录完成（进行中）、平台字幕获取成功、YouTube API 字幕获取成功、使用已有缓存（部分命中和全部命中两种）。

**后续决策更新（261008-completion-share）**：用户明确推翻“总结正文和任务完成合并为一条”。成功终态现在在原有接收通知之后发送完整 AI 总结，再追加一条分享短回执；正常成功每渠道总预算为 3 条，失败总预算为 2 条。失败终态仍一条 ❌，不恢复任何进度推送。详见 `docs/sessions/261008-completion-share/design.md`。

## 统一抬头

- 每条与任务相关的消息，第一行固定为 `{图标} [#{短编号}] {标题}`。
- 短编号取 `task_id` 去掉 `task_` 前缀后的前 6 个十六进制字符。
- 标题未知时用平台默认标题，没有平台默认标题时用链接。
- 飞书卡片的标题栏就用这一行，因为它会直接出现在会话列表预览里。
- 企业微信的消息也以这一行开头。
- 原模板第一行的 `## yyMMdd-HHmmss` 时间戳删掉，聊天软件本身就会显示时间。

## 完成消息可靠性

- 完成消息只走现有的终态通知发件箱（`task_terminal_notifications`）这一条发送路径。
- 消息正文在发送时由已落库的数据（`task_status` 和 `terminal_snapshot.result`）渲染，即时发送和重启后补发用同一个渲染函数，补发的内容和即时发送一致，都包含总结正文。
- 不再另外调用 `send_long_text` 发送总结。

## 偏慢提醒（第 2 张卡，门槛待用户确认）

生产实测数据：n305，2026-10-07，27 个 CapsWriter 任务，通过日志中的时长与任务编号关联，只取聚合值。

| 指标 | 50 分位 | 90 分位 | 95 分位 | 最大值 |
|---|---|---|---|---|
| 视频时长（秒） | 1973 | 17502 | 17502 | 17502 |
| 语音识别耗时 / 视频时长 | 0.03 | 0.10 | 0.105 | 3.28（93 秒视频识别了 306 秒，疑似服务端排队） |
| 语音识别耗时（秒） | 59 | 516 | 619 | 668 |
| 大模型阶段耗时（秒） | 94 | 165 | 178 | 216 |
| 下载耗时（秒） | 0.1 | 4.2 | 5.9 | 12.6 |

结论：

- 语音识别大约是实时速度的 10 到 30 倍，耗时与视频时长成正比；
- 大模型阶段耗时与视频时长基本无关，大致是一个常数；
- 批量提交时，排队等待是正常现象，不能计入偏慢判断。

方案：分阶段计时，排队时间不计入。

| 阶段 | 计时起点 → 终点 | 门槛 |
|---|---|---|
| 转录阶段 | 工作线程开始处理 → 转录完成 | 拿到时长后为 `600 + 0.15 × 时长` 秒；时长未知（含直播录制容器写的 0）时为 1800 秒 |
| 大模型阶段 | 大模型工作线程开始处理 → 任务终态 | 600 秒 |

规则：

- 每个任务最多提醒一次；
- 任务进入终态时取消计时；
- 计时器不持久化，服务重启后由现有的恢复扫描兜底；
- 把媒体时长写进 `observability`，供以后校准门槛。

样本局限：说话人识别（FunASR）路径没有样本，上线后再校准。

## 不变式

- 查看页和数据库中的进度字段照常更新，只删推送，不删状态写入。
- 两个渠道都保留，路由逻辑不变。
- 每个任务仍只有一行终态发件箱记录；失败终态一条，成功终态按同一行有序提交完整总结与分享回执两条逻辑消息（决策更新见 `docs/sessions/261008-completion-share/design.md`）。

## 通知不变式（N1–N4）

编号从 N 起，避免与 `terminal_status.py` 模块 docstring 里已有的 I1–I6（发件箱行级不变式）撞名。每条写明代码位置与锁定它的测试。

- **N1（每任务一条 outbox 记录；终态逻辑消息数量见后续决策）** 发件箱 `task_terminal_notifications` 每任务只有一行；失败终态发一条，成功终态由该行按序提交完整总结和分享回执两条（加上已接收，普通成功预算为 3 条），详见 `docs/sessions/261008-completion-share/design.md`。物理分段仍交给 `wecom-notifier`，库的分段缺口（飞书 interactive 卡片不分段、企业微信超长单行边界）见 #195。
  代码：`task_terminal_notifications.task_id` UNIQUE 约束（schema 层）；`src/video_transcript_api/api/services/terminal_status.py`（终态单出口与两个投递入口）。
  测试：`tests/unit/test_terminal_notification_outbox.py::TestOutboxSchema::test_task_id_unique_constraint`、`::test_outbox_schema_has_no_owner_columns`；单一 outbox 行及两投递入口互斥由同文件相关测试锁定，成功两条消息的顺序由 `tests/unit/test_notification_e2e_delivery.py` 锁定。
- **N2（✅ 正文来自快照，缺失时可见降级且不重试）** ✅ 消息正文来自 `terminal_snapshot.result`；`result` 缺失、不是 dict 或是空 dict 时，降级为抬头 + 可见提示（`⚠️ 总结未能载入，请在网页查看`）+ 查看链接，并打 `COMPLETION-BODY-MISSING` WARNING。缺失是确定性的，重试也补不回来，所以该行照常标记已发送、不重试；`result` 存在但渲染抛异常仍保持待发重试。
  代码：`terminal_status.py::_render_success_body`。
  测试：`tests/unit/test_terminal_notification_outbox.py::TestSuccessWithoutPersistedResultDegrades`（缺失、空 dict、其他渲染异常三路）。
- **N3（参数贯通）** `task_id`、`completion_body` 与成功回执从终态入口（`deliver_terminal_notification` 与 `deliver_pending_terminal_notifications`）一路贯通，经 `NotificationRouter.notify_task_status(**kwargs)` 到达企业微信和飞书两个渠道，不丢失、不改名。
  代码：`terminal_status.py::_emit_status_notification` → `src/video_transcript_api/utils/notifications/router.py::notify_task_status` → `src/video_transcript_api/utils/notifications/channel.py` 两个渠道的 `notify_task_status`。
  测试：`tests/unit/test_notification_e2e_delivery.py`（真实 Router + 真实渠道，仅替身最底层传输，对两个入口断言双渠道收到统一抬头和快照总结）。
- **N4（业务层不截断、不切分）** 业务层对完整完成正文不截断、不切分、不预拆卡片：`build_task_status_content` 把 `completion_body` 原样拼接进消息，超长时的物理分段交由 `wecom-notifier` 库；后续增加的分享回执为独立短消息，规则见 `docs/sessions/261008-completion-share/design.md`。
  代码：`channel.py::build_task_status_content`（拼接处注释即此约定）。
  测试：`tests/unit/test_notification_e2e_delivery.py`（快照里的总结全文出现在两个渠道收到的内容中）；2026-10-08 的 `9f68fe7f`（业务层飞书分卡）已 revert，回退即恢复此不变式。

## 已否决方案

- **详细程度开关**（`verbose` / `minimal`）：没有第二个使用场景，排查问题看日志即可。
- **从提交时刻开始计时**：批量提交时会让排在后面的任务全部误报。
- **用硬超时公式 `时长 × 4 + 120` 当偏慢门槛**：太晚，到那时已经直接失败了。
- **编辑或回复之前的消息**：飞书自定义机器人和企业微信 markdown_v2 都不支持。
- **业务层自行分卡或预切**（2026-10-08 revert `9f68fe7f`）：与 `wecom-notifier` 库的分段职责重复，用户拍板超长内容的分段只交给库负责（缺口见 #195）。
- **截断完成正文里的总结**：用户要在聊天里看到具体总结，截断后正文失去价值；总结缺失时改为可见提示 + 链接（N2）。
