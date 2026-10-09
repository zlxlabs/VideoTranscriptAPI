---
lane: local-upload
id: M3
slug: worker
status: 已完成
owner: delegate implementer dlg-20261008-181551-f0aaf6
order: 3
priority: 高
depends_on: [local-upload/M1, local-upload/M2]
merged_pr: null
---

# 里程碑进度：local-upload/M3：本地输入进入既有转录 worker

- **预期产出**：本地 owned media 按唯一 root task 接入既有 `process_transcription`、ASR、LLM、通知、终态与临时文件生命周期。
- **当前范围**：C 卡；不另建 ASR/LLM/通知状态机，不实现第二套重试/预算，不把公开 upload token 写入旧 `task_status.view_token`。
- **关键决策**：复用现有处理链；root首个 success/failed 起算固定期限，子任务/重启不续期；原媒体及中间文件仍按既有 finally 清理。
- **推进前必须拿到的证据**：
  - [x] `tests/integration/test_local_upload_lifecycle.py` 通过真实 dispatcher、共享 `process_transcription`、实际 `Transcriber` 与隔离 CapsWriter 文件 producer，断言映射路径/字节、时长、选项、root/终态、public result、通知回执和 LLM 前清理；URL parser/downloader 被断言不可达。
  - [x] `tests/unit/test_capswriter_contract.py` 锁 invalid producer contract；`test_upload_worker.py` 锁具名失败状态；终态成功/失败/启动孤儿、子任务迟到写、撤销/到期与受理竞态均由 SQLite/HTTP 消费者测试覆盖。
  - [x] queue/数据库/ASR 失败以现有 worker 失败与 `UPLOAD_PROCESSING_FAILED` 显式可见；临时目录删除失败不再被 local-upload 路径吞掉；未新增重试、fallback 或第二状态机。
  - [x] 环境测试在裸 shell（变量未设）及 dispatch 专属 transient systemd unit（显式 false，测试同时通过真子进程验证缺省拒读/显式启用）；未触碰生产配置/服务。Hosted CI 结论由 lead 按任务卡取证，draft 状态不计作门禁通过。
- **完成条件**：真实处理者消费的是接收器写出的媒体与选项，阶段与 URL 路径保持一致；正文资格和任务进度分离。
