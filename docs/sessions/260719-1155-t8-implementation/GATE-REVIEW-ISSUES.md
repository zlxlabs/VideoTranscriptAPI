# Gate 评审可靠性故障证据与修复建议（2026-07-20）

背景：feat/chapters-wiring 拆分为 8 个 stacked PR（#15-#23）跑 gate CI。
过程中 claude-glm 主审的 verdict 可靠性故障共 **6 次**（12 次评审中），分三种形态。
所有结论均可由对应 run 的 `codex-review-result.json` artifact 复核。

## 故障形态一：placeholder verdict（4 次）

verdict JSON 字面为 `{"verdict": "pass"|"fail", "summary": "placeholder", "findings": []}`，
schema 合法但内容为空。两个方向都出现过：

| PR | run | verdict | 后果 |
|---|---|---|---|
| #15 | 29739180039 | fail | 误判失败，白跑一轮 |
| #17 | 29741943644 | **pass** | **未审即放行**（我 rerun 追到真实评审） |
| #16 | 29740334937 | fail | 误判失败，rerun 后真实 pass |
| #22 | （首轮） | **pass** | **未审即放行**（我 rerun 追到真实评审） |

特征：review turn 约 60s 速回（正常 5-13 分钟），疑似 GLM API 瞬态失败被
adapter 包成 schema 合法的空 verdict。

## 故障形态二：verdict 与 findings 自相矛盾（2 次）

评审内容明确干净通过，verdict 字段却填 fail：

- PR #18 run 29745157075（2 轮连续）：summary「未发现任何 blocker 或 major」、
  findings 仅 2 nit、8 项集成校验全过，verdict=fail。两轮成本约 $8.8。
  第二轮疑被首轮 fail verdict 锚定复读（previous_review 上下文含前轮 fail）。
  处理：审计豁免（label + waiver comment + close/reopen）。
- PR #19 run（修复后轮，head 4a38572）：summary「最终集成审查通过」、
  4 条前轮 major 全部核实已修复、仅剩 2 minor + 1 nit，verdict=fail。
  处理：审计豁免。

## 故障形态三：撤回性 finding（1 次，PR #21 首轮）

2 条 major findings 的 evidence 部分逐路径验证后自行写明「此处无实际 bug，
撤回此 finding」，但仍以 major 级别留在 findings 列表中并决定 verdict=fail。
（本地独立复核证实算法正确，且评审最初建议的改法反而会引入真 bug。）

## 根因与修复建议

validate_verdict.py 只做 schema 校验（字段齐/枚举合法），不校验 verdict、
summary、findings 三者的逻辑一致性。schema 合法 ≠ 逻辑自洽。

建议在 validate_verdict 层增加**确定性一致性规则**（纯代码，不需要第二个模型）：

1. `verdict=fail` 且 findings 中无 blocker/major → 评审无效
2. `verdict=pass` 且 findings 中有 blocker/major → 评审无效（最危险方向）
3. summary 为空/「placeholder」/模板占位 → 评审无效
4. finding 文本自声明撤回（如含「撤回此 finding」）→ 不计入 verdict 判定

判「评审无效」后的动作：不当 PR 结论，当**本次评审失败**，走既有 failover
链（claude-glm → codex-sub）重审——这正是 failover 的设计本意，只是目前
failover 只在 adapter 报错/JSON 不合法时触发，对「合法但胡说」失明。

另：codex-sub 兜底评审质量实测很高（#19 抓出 4 条真 major、#20 抓出 3 条、
#22 复审逐条核实），可作为 claude-glm 修复期间的临时主审考虑。

## 附带：本次 stacked 拆分的经验

- 按行数机械切分会把「实现」与「接线/消费者」切到相邻 PR，产生
  scope-mismatch 与 dead-code 评审阻力（#15、#20、#21 三次）。教训：
  拆分边界应对齐「功能完整交付」（实现+接线+测试同 PR），行数上限内优先
  保完整性。
- rebase 链式修复（cherry-pick 后续已有修复 + patch-id 自动去重）在本次
  实践中验证可行，108 提交全链 rebase 三次零冲突。
