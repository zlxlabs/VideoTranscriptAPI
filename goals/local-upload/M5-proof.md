---
lane: local-upload
id: M5
slug: proof
status: 未开始
owner: lead
order: 5
priority: 高
depends_on: [local-upload/M1, local-upload/M2, local-upload/M3, local-upload/M4]
merged_pr: null
---

# 里程碑进度：local-upload/M5：全链路安全与兼容性证明

- **预期产出**：从真实 HTTP producer 到 owned bytes、task/queue、既有处理链、Resolver、公正文、本人 history 与 cleanup 的端到端证据。
- **当前范围**：E 卡，依赖 M4 完整网页行为与真实 API producer；M4 尚受 UI registry 登记阻塞，因此 M5 尚未开始。整理方案、helper 单测或邻侧 response 不算实际消费证明。
- **关键决策**：每项不变式必须指向真实生产代码与能被 mutation 打红的测试；跨文件/进程边界保存 producer 实际 payload。
- **推进前必须拿到的证据**：
  - [ ] 真 FastAPI、临时 SQLite/文件系统、真实 dispatcher、Resolver 与现有 consumer；断言 HTTP/body、subprocess argv/env、写入文件等实际 producer 输出。
  - [ ] 同意图恰好一个受理，两次明确上传恰好两个独立成果；短期、never、expiry、revoke、disabled 全有 active positive control。
  - [ ] controlled pause 证明撤销/迟到发布与清理/发布两顺序；每种均由 fresh consumer 检查关闭不复活和有效成果不丢。
  - [ ] URL dedup/read/history/notification 与全局 cleanup 整文件回归；`make test` 通过。CI只看 run/job `conclusion`，skip 不算通过。
  - [ ] CI、裸 shell、实际 systemd 消费环境分别跑环境相关测试；systemd 环境必须是本地 consumer 实际运行的 unit。
- **完成条件**：任何“已覆盖”都有执行证据；矛盾/未知机制重新开受影响设计，不以 fixture 命名掩盖差异。
