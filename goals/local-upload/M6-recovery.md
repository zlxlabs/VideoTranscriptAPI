---
lane: local-upload
id: M6
slug: recovery
status: 未开始
owner: delegate implementer dlg-20261008-094534-0fd910
order: 6
priority: 高
depends_on: [local-upload/M1, local-upload/M2, local-upload/M3, local-upload/M4, local-upload/M5]
merged_pr: null
---

# 里程碑进度：local-upload/M6：恢复域、单向撤销与容量恢复

- **预期产出**：证明独立恢复开关、cache.db/正文/audit/share记录完整恢复与容量阈值；未证明前上传读写关闭。
- **当前范围**：F 卡，生产前置证明；A 不做恢复演练、容量实测或部署授权。
- **关键决策**：`VTA_UPLOADS_ENABLED` 在数据卷外；可信撤销状态/完整恢复域不能证明时保持关闭；长期成果不能通过删除腾容量。
- **推进前必须拿到的证据**：
  - [ ] 用与目标服务一致但隔离的真实 systemd unit、身份、mount、配置与数据格式演练完整备份/恢复；启动新服务前检查开关关闭。仅获授权后操作。
  - [ ] fresh Resolver 证明有效30d/never正文和必要解析记录保留、已撤销/关闭状态不复活；缺一个恢复域时拒绝打开；回滚不能以旧卷重开分享。
  - [ ] 在实际磁盘配额、ASR并发和URL负载下测 max_file_mib、max_media_hours、receive_concurrency、upload_temp_budget_mib；保留真实文件/调用证据，拒绝用示例数字冒充实测。
  - [ ] 容量不足拒绝新受理且可见；中断/孤儿清理有界，原媒体仍按既有处理阶段清理；不删除有效成果释放空间。
  - [ ] 单列CI、裸 shell、获准 systemd unit 环境结果；文档、配置值或模拟介质不能替代恢复演练。
- **完成条件**：证明完整、通过独立审查并经用户/部署授权后才能评估启用；任何关键证明缺失都维持默认关闭。
