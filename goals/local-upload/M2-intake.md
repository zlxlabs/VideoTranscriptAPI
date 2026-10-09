---
lane: local-upload
id: M2
slug: intake
status: 已完成
owner: lead
order: 2
priority: 高
depends_on: [local-upload/M1]
merged_pr: 203
---

# 里程碑进度：local-upload/M2：认证接收与幂等回执

- **预期产出**：Bearer 认证的能力查询、raw octet-stream 接收、有限 metadata header、真实额度准入、owner/key receipt 与诚实 HTTP 状态。
- **当前范围**：B 卡真实 HTTP 接收已随 PR #203 合并至主干（merge commit `c7b38563eb2f06f5ca36c06c6c7da7de9c5e9995`）；不把 A 的 SQLite store 方法误报为 HTTP 接收端。
- **关键决策**：沿用 `docs/sessions/261008-local-upload/design.md` 的 wire 合同；新正式受理截止为 key 创建时间+24h（到截止时拒绝），未来容差5分钟；`receiving` 不等于 accepted/202，同意图只回原receipt、不消费新 body、不重入队，变化 metadata 409。root/accepted与一次 `put_nowait` 位于同一个无await SQLite临界步骤，commit成功后才回202；SQL和queue并非共同事务，commit失败形成的stale项由dispatcher durable admission gate丢弃。
- **推进前必须拿到的证据**：
  - [x] 真实 FastAPI/ASGI 与临时 cache.db 上由 client 发 raw 文件字节及 headers；锁定实际落盘字节、服务端 SHA-256、Unicode metadata、root row 与 dispatcher 实际消费路径。producer fixture 保留在 `tests/integration/test_upload_intake.py`。
  - [x] 缺/错 Bearer、默认关闭、额度未知、非 octet-stream、声明/实收超限、receiver/inflight/free-space 满载均在 body/file/ASR 边界前显式拒绝。
  - [x] 同 key 重放/变化 metadata、相同字节新 key、真实 ASGI 断开、队列满、SQLite commit fault（真实队列已发stale payload且dispatcher丢弃）、queue put fault、窗口跨期与丢失202回执重查均有 producer→consumer 测试；same-intent 不读 body/不重入队。
  - [x] receiving cleanup/accept 两个可控真实SQLite+文件顺序均锁定：accept先赢时源字节保留；cleanup先退休时accept失败且root不创建，之后才unlink已退休路径。
  - [x] 未设置VTA开关的真实consumer测试在`env -i`裸shell与隔离的临时systemd user unit分别运行成功；未改真实systemd/service或生产环境。
  - [ ] Hosted CI及实际生产unit环境传递仍由lead验收；本地隔离验证不等于生产部署/容量证明。
- **完成条件**：B 实现已合并，status「已完成」只表示代码与实现阶段落地主干。PR #203 的真实 gate 主审结论为 SUCCESS；实际生产 unit 环境变量传递、容量证明与部署授权仍未完成，生产仍关闭。
