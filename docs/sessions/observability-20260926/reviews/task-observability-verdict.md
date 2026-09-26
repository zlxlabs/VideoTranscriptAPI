# 任务观测与只读复盘审查

failure-visibility: clean

- 风险等级：personal；最终代码冻结为 `c42f9c2db447871bb357f5e4b91b1c062f479e06`。
- 独立审查覆盖初版与阶段修复；最后的来源合并增量由state_architecture审查`53438561..c42f9c2d`，主脑Codex对照真实生产数据复核。
- 创建时间、白名单观测沿既有终态不可变快照归档；metadata与转录保存失败区间不再假报成功；笔记产物完整性与任务收尾异常分离。
- audit唯一整数v5/v6及相应字段必须匹配，只读CLI不迁移/创建坏路径，错误非零。
- 先合并完整cache/audit任务，再由唯一时间窗判定选择任务。有效audit起点优先，cache补缺；缺失与零严格区分。
- 正缓存命中分别统计，二者均无正值为unknown；并行模型调用累计耗时不冒充墙钟。
- 测试：完整unit/integration **3105 passed**；真实producer→SQLite→CLI与已知坏态反向转红，详见`tests/integration/test_task_observability.py`、`tests/unit/test_task_observability_report.py`及设计记录中的测试映射。
- 生产固定UTC窗口`2026-08-27T03:25:08Z`至`2026-09-26T03:25:08Z`：冻结脚本与独立只读SQL一致，391窗内任务（372成功/19失败），4227窗外有起点任务，未归属0。未写生产或迁移。旧观测字段缺失不能倒推填零。
- 正式CI `run#36222103443` 的quality/primary/ocr/gate全部SUCCESS，PR90已合并至 `aae5622df9bb75001d7e2b5a33484c41a3d62fad`；本记录不声明已经部署。
