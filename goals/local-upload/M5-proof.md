---
lane: local-upload
id: M5
slug: proof
status: 进行中
owner: lead
order: 5
priority: 高
depends_on: [local-upload/M1, local-upload/M2, local-upload/M3, local-upload/M4]
merged_pr: null
---

# 里程碑进度：local-upload/M5：全链路安全与兼容性证明

- **预期产出**：从真实 HTTP producer 到 owned bytes、task/queue、既有处理链、Resolver、公正文、本人 history 与 cleanup 的端到端证据。
- **当前范围**：E 卡已基于 PR #209 的真实合并产物实施，PR #215 测试增量已交回，本地完整独立复演已完成，等待正式 CI/合并。M4 网页源码与 UI registry 前置已满足；新增测试连接真实网页、FastAPI、SQLite、dispatcher 和转录/模型 SDK，仅最后外部服务使用 loopback。下列清单仍逐项验收，不将作者报告、草稿绿、helper 单测或邻侧 response 当完整通过。
- **关键决策**：每项不变式必须指向真实生产代码与能被 mutation 打红的测试；跨文件/进程边界保存 producer 实际 payload。
- **推进前必须拿到的证据**：
  - [x] 真 FastAPI、临时 SQLite/文件系统、真实 dispatcher、Resolver 与现有 consumer；断言 HTTP/body、subprocess argv/env、写入文件等实际 producer 输出。
  - [x] 同意图恰好一个受理，两次明确上传恰好两个独立成果；短期、never、expiry、revoke、disabled 全有 active positive control。
  - [x] controlled pause 证明撤销/迟到发布与清理/发布两顺序；每种均由 fresh consumer 检查关闭不复活和有效成果不丢。
  - [ ] URL dedup/read/history/notification 与全局 cleanup 整文件回归；`make test` 通过。CI只看 run/job `conclusion`，skip 不算通过。
  - [ ] CI、裸 shell、实际 systemd 消费环境分别跑环境相关测试；systemd 环境必须是本地 consumer 实际运行的 unit。
- **完成条件**：任何“已覆盖”都有执行证据；矛盾/未知机制重新开受影响设计，不以 fixture 命名掩盖差异。
- **边界**：真实 ASR/LLM 模型质量、生产容量/反代/完整恢复域及生产镜像身份不由本地 loopback 测试认证，继续由 M6 和用户部署授权约束。

- **本地证据边界**：基于测试提交 `b50c861ef276248db087466992d549cc8edc90fe`，真实 Chromium→FastAPI→SQLite→共享 SDK→正文/history/通知/revoke 与五组控序已在隔离裸 shell 和实际临时 systemd 消费环境验证，完整 pytest 入口、网页 186 项、浏览器 17 项通过。正式 CI 尚未运行，里程碑仍进行中，不将本地结果视为模型质量或生产验收。
