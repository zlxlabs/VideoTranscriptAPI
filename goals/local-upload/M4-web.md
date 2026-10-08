---
lane: local-upload
id: M4
slug: web
status: 未开始
owner: delegate implementer dlg-20261008-094534-0fd910
order: 4
priority: 中
depends_on: [local-upload/M1, local-upload/M2, local-upload/M3]
merged_pr: null
---

# 里程碑进度：local-upload/M4：上传、回执与本人历史网页

- **预期产出**：原生单文件 picker/drop、metadata headers、期限选择、真实 receipt/处理中/结果状态与本人 history source filter。
- **当前范围**：D 卡；不得从 mock-only fixture 发明 wire 形状，不改旧 URL 任务页面规则。
- **关键决策**：沿用设计文件中的 HTTP 字段；默认30天、长期显式选择 never；进度继续走既有 task API；source URL 仅展示，不 fetch。
- **推进前必须拿到的证据**：
  - [ ] 浏览器/harness 调用真实 API，断言 producer 发出的 raw Blob 字节、headers 和完整服务端 receipt；保留实际请求 fixture。
  - [ ] picker/drop 同路；100% 传输不等于正式接受；覆盖 401、网络未知、duplicate receipt、处理中、失败、关闭和到期状态。
  - [ ] 真实 history query 先按 source 过滤再分页；断言实际 ID 集、total、页边界与 owner 隔离；未实现 bodygate 时无摘要旁路。
  - [ ] filename/source 输入经真实模板安全渲染；恶意路径、Unicode、引号、无效 URL、键盘和小屏测试。CI browser/API 环境运行；systemd验证仅随另行授权部署。
- **完成条件**：浏览器状态只反映服务端 receipt/进度事实；失效 token 仍由服务端拒绝，文案不许承诺回收通知副本。
