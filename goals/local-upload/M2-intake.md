---
lane: local-upload
id: M2
slug: intake
status: 未开始
owner: delegate implementer dlg-20261008-094534-0fd910
order: 2
priority: 高
depends_on: [local-upload/M1]
merged_pr: null
---

# 里程碑进度：local-upload/M2：认证接收与幂等回执

- **预期产出**：Bearer 认证的能力查询、raw octet-stream 接收、有限 metadata header、真实额度准入、owner/key receipt 与诚实 HTTP 状态。
- **当前范围**：B 卡；不把 A 的 SQLite store 方法误报为已存在 HTTP 接收端。
- **关键决策**：沿用 `docs/sessions/261008-local-upload/design.md` 的 wire 合同；key 窗口24小时、未来容差5分钟；同意图不消费新 body、不重入队，变化 metadata 409。
- **推进前必须拿到的证据**：
  - [ ] 真实 FastAPI/ASGI 与临时 cache.db 上由 client 发 raw 文件字节及所有 headers；断言 handler 实收字节、metadata 解码、commit receipt 与真实 task/queue producer 参数。跨进程 payload 保留 producer fixture。
  - [ ] 无开关、关闭开关、任一额度缺失/无效时实际返回 disabled 并拒绝接收；四项额度全为正有限数才测 enabled。生产测量前不填真实容量结论。
  - [ ] 同 key 重放时 consumer 证明没有读取新 body、没有第二次 queue 入队；查询只读回原 receipt；丢回执不自动生成新 key/重传。
  - [ ] 在 CI、无会话身份裸 shell 和获准隔离 systemd unit 分别验证真实消费环境变量与接收行为。systemd/生产改动另需部署授权。
- **完成条件**：202 只在持久正式接受后返回；未知/失败不假成功；queue/SQLite 交接窗口由真实入口测试锁死，不加 retry/fallback/outbox。
