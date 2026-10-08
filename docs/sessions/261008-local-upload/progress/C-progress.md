## 2026-10-09 · 首个 worker 单元

- **当前阶段**：implementing；首个独立 worker 单元已通过，尚未提交其余 C 增量。
- **本段结论**：新增真实 dispatcher→持久 admission/media mapping→共享转录函数单元，断言映射文件字节、任务 id、媒体 id 和处理选项；`uv run --frozen pytest -q tests/unit/test_upload_worker.py` 通过。
- **关键决策与已否决方案**：转录输入严格由 SQLite 映射提供，队列 URL 只作展示值；沿用 B 已持久化的处理选项，不加 URL 下载或新处理入口。
- **下一步唯一动作**：提交并推送该单元，然后继续生命周期与终态期限实现。

## 2026-10-09 · worker 生命周期、终态与公开消费

- **当前阶段**：implementing；本地上传 ASR/LLM 路径、终态锚点、读取/重处理、历史与通知生产者已接通并有真实 SQLite/dispatcher 测试，尚未全量验收。
- **本段结论**：root 首次 success/failed/启动孤儿失败固定写入 `terminal_at`/期限；真实 `process_task_queue → process_transcription → Transcriber → LLM` fixture 断言实际文件字节、映射、选项、原媒体在 LLM 前删除、公开成果与最终通知，三重处理路由与读写关闭顺序也被集成测试锁定。
- **关键决策与已否决方案**：保留 B 的持久 media-path 字段契约，仅删除源文件与任务临时目录；child 旧 token 列恒为空，授权在 `BEGIN IMMEDIATE` 中随 child INSERT 再核验，撤销不取消已受理工作或移除已发通知。
- **下一步唯一动作**：运行任务卡 Narrow-Verify 与 `make test`，检查差异后提交推送并交 lead 接 CI。

## 2026-10-09 · 本地验收完成

- **当前阶段**：implementing；Narrow-Verify 37 passed，`make test` 全量入口通过（3 skipped）；本地裸 shell 与 dispatch 专属 transient systemd unit 的真实子进程环境探针通过，Hosted CI 由 lead 后续取证。
- **本段结论**：HTTP upload accepted 通知、终态补发到最终 HTTP JSON、三条 owner 重处理路由、全部 public body/summary 出口、历史过滤/期限原因、terminal clock write-once 与真实 worker/ASR 接线均由新增测试覆盖；当前分支待提交推送。
- **关键决策与已否决方案**：不改已合并 B 的持久 admission/media-path 契约；public token 仅用于上传记录和临时投递副本，root/child task 与 audit alias 仍为空。
- **下一步唯一动作**：提交并推送本卡分支的 C 实现，保持 PR draft，由 lead 接管 hosted CI / 独立审查。
