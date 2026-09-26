# 内容质量人工评估

本流程汇总人对候选内容的评价。命令只检查记录格式并做计数，不读取文本来判断语义。仓库示例全部是 synthetic，只验证量表与算术，不代表生产内容准确率。

## 人工量表

同一输入、同一候选版本逐项记录 `correct`（核对无误）、`error`（确认有错）、`unknown`（已看但无法判定）或 `unreviewed`（尚未评）。未知与未评都不算正确；错误必须记输入和候选输出两处证据位置。

| 维度 | 核对问题 |
| --- | --- |
| numbers | 数字、日期、金额、数量是否一致？ |
| negation | 否定和肯否方向是否保留？ |
| conditions | 适用条件、例外和限定是否保留？ |
| speaker_attribution | 说话人或观点归属是否正确？ |
| key_omissions | 是否遗漏了影响理解或行动的关键内容？ |

每条样本记录来源类型、输入引用和文本、候选输出引用和文本、候选版本和生成日期。无法核实时填 `unknown` 并写原因；日期用 `YYYY-MM-DD`。错误证据需具体到输入文本与候选输出文本中的位置。仓库 fixture 内嵌的文本全为 synthetic，便于人工直接核对。真实材料应保存在仓库外的受限目录，评审文件也按私有产物管理。

## 采样和盲评

1. 先定常读材料的采样范围与规则，例如固定时间段内按来源类型分层抽取；保留入选清单和未入选数量，避免只挑容易的例子。
2. 在受限目录冻结输入材料、来源定位符或内容哈希，以及每个候选的完整输出、生成版本、日期和必要的生成配置。版本或日期无法验证时记 `unknown` 和原因。
3. 比较改前改后时使用同一批输入与相同切分边界，随机化候选标签；评审者先独立完成量表，再解盲。不要把不同来源或不同样本集合的分数直接当作前后变化。
4. 保存每位评审者的原始表和报告快照。抽查来源定位和两侧证据；有分歧时保留各自记录，另行人工复核，不以多数票冒充客观真值。

## 文件与命令

输入为 UTF-8 JSON，格式可直接参考 [synthetic 示例](../../tests/fixtures/content_quality/synthetic_review.json)。每个文件只允许一个 `reviewer`；样本含来源、输入引用和文本及候选列表。候选含版本、日期、输出引用和文本及五维评价。允许的来源为 `synthetic`、`youtube`、`tiktok`、`bilibili`、`podcast`、`other`。缺字段、未知枚举、重复 `sample_id + candidate version + reviewer` 或无效证据会以非零退出；同文件混合评审者会被拒绝。分别保存评审者文件；若评审者结论不同，保留并人工对照各自原表，不做汇总、投票或多数票真值。

```bash
python scripts/content_quality_report.py --reviews path/to/reviews.json
```

命令只在标准输出写汇总 JSON。每个版本列样本数和各维 `reviewed`、`scored`、`correct`、`errors`、`unknown`、`unreviewed`、`error_rate`；`reviewed` 包含 `correct`、`error`、`unknown`，`scored` 仅含 `correct` 与 `error`，错误率为 `errors / scored`，分母为零时为 `null`。版本或日期不能核实时使用字面值 `unknown` 并填写说明，报告会列入 `metadata_notes`；漏掉必填字段则拒收。

评审材料可能含私人正文。默认放在仓库外的访问受限目录或受控存储，例如 `~/.local/share/video-transcript-api/quality-reviews/`；不要默认提交原文、候选输出或证据。需要共享时仅提交脱敏量表或汇总，并先确认其中没有可还原正文的信息。

## 报告边界

报告是人工已完成评价的计数，不是自动语义评级。`unknown` 和 `unreviewed` 不进入错误率分母；样本量、抽样偏差、评审者差异和量表解释都会影响结果。一次 before/after 分数差异本身不能证明“质量提升”；需披露样本范围、人工步骤、版本信息和未评比例，并复核证据。仓库 synthetic 示例的数字只说明报表链可运行，真实内容质量仍未知。
