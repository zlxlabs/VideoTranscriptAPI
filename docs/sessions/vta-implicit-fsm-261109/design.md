# DESIGN-note：隐式状态机跨边界缺陷根治（2026-11-09）

## 目标

转写结果不再静默出错：不会拿到别的任务的转写、不会把拼坏的下载文件当成功、分 P 视频不会串缓存、页面和通知上的「校对完成度」说的是同一件事、读到的缓存不会新旧混代。

## 非目标

- 不给 93 个隐式状态机套通用状态机框架；约 70 个函数内重试/降级链不改。
- 不加 task_status 运行时转移 CAS（两位顾问一致砍掉：无非法转移可达证据，且「非法转移返回 False」会被 terminal_status.py 当成终态 CAS 失败而跳过通知、留下悬置任务）。只补转移文档。
- 不改审计快照 UPSERT（无可达覆盖序列证据；归档来源均为终态且有终态黏性）。
- 不迁移历史持久化的 calibration_status（已存 full 不重写）。
- 不碰 utils/notifications/router.py（他会话 PR #296 在途）。

## 为什么不是分区 / 删除 / 约定

- **分区**：FunASR 已是每任务一连接；问题不在共享，而在不核对回复身份——分区消不掉。分 P 身份碰撞正是「没分区」：用约定把 (BV,p) 编进 media_id 即完成分区。
- **删除**：generic 下载的断点续传整段删除——每次重试从零完整下载，中断代价是重下，换来「续传状态机」整体消失，不再需要 206/Content-Range/If-Range 校验（codex 建议，采纳）。
- **约定**：分 P 身份用 media_id 命名约定：p=1 保持原 BV（旧缓存无需迁移），p>1 为 `<BV>_p<N>`；不改表结构。

## 方案要点与已否决方案

- **要点**：五类根因各打一处，在边界上校验并 fail fast；同一事实单一函数。
  - C1 FunASR 回复身份绑定（funasr_client.py）
  - C2 下载完整性：删除 generic 续传；base 流式下载 Content-Length 不符即失败
  - C3 校对完成度单一派生函数（含通知第四处），口径 = 有低质量/低覆盖不得 FULL，零有效 LLM 贡献为 NONE，零单元为 NONE
  - C4 小修：BusyLock 重入抛错；小宇宙 token 原子保存且失败上抛；task_status 转移文档补 processing 初始态；本文件入库
  - C5 B 站分 P 身份端到端（url_parser、bilibili 下载器、TikHub 按分 P 选 cid）
  - C6 转写读取失败上抛、空转写失败；共享 workspace 的 output_base 加任务唯一后缀
  - C7 本地上传「分享是否有效」回执改用权威函数，fail-closed
  - C8 持久缓存正文/侧车整组一致（先真实链路红测复现，再修）
- **已否决**：
  - task_status ALLOWED_TRANSITIONS 运行时 CAS（见非目标）
  - generic 续传保留并补 206/Content-Range 校验（实现更大，错误 206 的处理易引入新静默错）
  - 「txt 最后写=整组提交标记」（旧 txt 仍在时无法标记新一轮；真正长期读边界在 save_cache）
  - 结构化旧口径「partial 仍 FULL」（顾问 codex 探针：corrections 全空仍 FULL；违背枚举注释与纯文本测试）
  - 为 F10 两个清理 SQL 强行统一（服务不同清理上下文）

## 关键不变式

1. [实测] FunASR 客户端只接受 task_id 等于本任务的结果；异 id 推送忽略、缺 id/错位 batch 报协议错误、completed 空结果失败 —— funasr_client.py，C1 测试锁死。
2. [实测] generic 下载每次尝试以 `wb` 从 0 写；不发 Range —— generic.py，C2 测试锁死。
3. [实测] calibration_status 全仓只有一个派生函数 —— utils/llm_status.py，C3 测试锁死四个调用点。
4. [实测] 同一 BV 不同分 P 的 media_id 不同，p=1 与历史一致 —— url_parser.py/bilibili.py，C5 测试锁死。
5. [推断，C8 验证] get_cache 读不到「新正文 + 旧侧车」。

## 待验证前提

1. [推断] C8 的混代窗口在真实 save_cache→get_cache 链路可复现；复现不了 C8 只交报告。
2. [推断] FunASR 服务端正常情况下不会回别的任务条目（只能代码侧防御，无法验证服务端）。

## 验收路径

1. 入口：各卡 TDD 红测在 base b54657b4 上红、修后绿；`make test` 全绿。
2. 步骤：合并后在部署环境提交一个 B 站多 P 链接的 p=2，确认标题/内容为第 2P；提交一个普通任务确认页面校对横幅正常。
3. 预期：分 P 不串；无新的 ERROR 日志。

出处：三份只读审计报告（dlg-20261109-050619-6933bb / 050622-6b422a / 050623-57c2a3）；consult codex + kimi 结论均为「降级」。
