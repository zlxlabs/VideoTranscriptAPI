---
lane: local-upload
id: M3
slug: worker
status: 未开始
owner: delegate implementer dlg-20261008-094534-0fd910
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
  - [ ] 真实 `process_transcription` 消费临时文件与 SQLite root；只替换 ASR/LLM/通知外部边缘。保留 producer fixture 并断言实际 ASR 参数/argv、文件字节、root id、终态与通知 payload。
  - [ ] success/failure/invalid result 均有诚实终态；stop-share 不取消已接受处理、不专属删正文；重启/迟到写不能改变 root TTL 或复活撤销。
  - [ ] queue、数据库、ASR/LLM 失败通过具名状态/日志可见；不吞错、不假成功、不自动重试或切换引擎。
  - [ ] 在 CI、裸 shell、获准 systemd 隔离 unit 测真实环境变量和 worker 启动消费；不碰生产配置/数据。
- **完成条件**：真实处理者消费的是接收器写出的媒体与选项，阶段与 URL 路径保持一致；正文资格和任务进度分离。
