---
lane: local-upload
id: M2
slug: intake
status: 已实现待独立审查与CI
owner: delegate implementer dlg-20261008-130923-6b2088
order: 2
priority: 高
depends_on: [local-upload/M1]
merged_pr: null
---

# 里程碑进度：local-upload/M2：认证接收与幂等回执

- **预期产出**：Bearer 认证的能力查询、raw octet-stream 接收、有限 metadata header、真实额度准入、owner/key receipt 与诚实 HTTP 状态。
- **当前范围**：B 卡；不把 A 的 SQLite store 方法误报为已存在 HTTP 接收端。
- **关键决策**：沿用 `docs/sessions/261008-local-upload/design.md` 的 wire 合同；新正式受理截止为 key 创建时间+24h（到截止时拒绝），未来容差5分钟；`receiving` 不等于 accepted/202，同意图只回原receipt、不消费新 body、不重入队，变化 metadata 409。
- **推进前必须拿到的证据**：
  - [x] 真实 FastAPI/ASGI 与临时 cache.db 上由 client 发 raw 文件字节及 headers；锁定实际落盘字节、服务端 SHA-256、Unicode metadata、root row 与 dispatcher 实际消费路径。producer fixture 保留在 `tests/integration/test_upload_intake.py`。
  - [x] 缺/错 Bearer、默认关闭、额度未知、非 octet-stream、声明/实收超限、receiver/inflight/free-space 满载均在 body/file/ASR 边界前显式拒绝。
  - [x] 同 key 重放/变化 metadata、相同字节新 key、真实 ASGI 断开、队列满、SQLite commit fault、queue put fault、窗口跨期与丢失202回执重查均有 producer→consumer 测试；same-intent 不读 body/不重入队。
  - [ ] 最终CI及系统级部署 unit 的环境传递仍由 lead 验收；未改真实 systemd/service 或生产环境。本地隔离裸shell验证待最终阶段执行。
- **完成条件**：本地代码与测试已覆盖 202/失败边界；正式合并、CI 与真实部署环境变量/容量证明由 lead 后续托管，生产仍关闭。
