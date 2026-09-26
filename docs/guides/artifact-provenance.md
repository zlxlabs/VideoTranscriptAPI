# LLM 阅读产物来源记录

## 目标与边界

`llm_status.json` 为校对和总结两层保存本轮来源记录。来源记录回答“这份当前 TXT 字节是否与记录一致”，不回答“内容是否正确”，也不证明当前输入与生成时相同、能够重放 LLM 调用或实际使用了哪个模型。不会保存原文、完整提示词、密钥或其他媒体缓存内容。

校对的 `source_fingerprint` 是调用校对处理器前冻结的、版本化输入 envelope 的 SHA-256：包括本轮实际传给处理器的文本或结构化内容、标题/作者/描述/平台/媒体 ID、选项和请求模型配置。总结的 envelope 在总结处理器调用前冻结，包括最终 `calibrated_text`、实际 `speaker_count`、标题/作者/描述、实际传入的 `transcription_data` 和请求模型配置。两者都用按键排序、固定 JSON 分隔符和 UTF-8 编码计算；字符串逐字保留，列表顺序保留，不做章节裁剪、拼接或其他内容归一化。因此这是整份输入 envelope 的指纹，不是正文原始字节 hash。字典键顺序变化不影响指纹，输入值变化会改变指纹。

每层记录包含来源指纹、处理配方版本、最终 TXT 文件字节的 SHA-256、UTC 落盘时间、`GIT_SHA`（缺失时为 `unknown`）、本层 `requested_model` 标签和生成方式。其他选择的模型配置只进入输入指纹，不完整持久化。`actual_model` 与实际提示词版本目前无法可靠确认，明确记为 `unknown`，因此记录不支持完整重放。短文本总结若复制校对文本，会标为复制结果；全降级校对会标为格式化兜底，不表示 LLM 成功。未写入文件的失败、禁用和跳过层不会获得新的来源记录。只重写一层时另一层的来源记录和时间保留；旧调用方重写文件但没有新来源记录时，旧记录会被清除。

## 只读查验

对一个媒体缓存叶子目录运行：

```bash
python scripts/artifact_provenance_report.py --cache-dir /path/to/cache/platform/year/month/media-id
```

命令只读取该目录下的 `llm_status.json`、`llm_calibrated.txt` 和 `llm_summary.txt`，不会递归遍历缓存根目录。每层状态为 `verified_output`、`mismatch` 或 `unknown`：缺少来源记录为 `unknown`，存在记录但 TXT 缺失或字节 hash 不符为 `mismatch`，字节 hash 相符为 `verified_output`。损坏的 `llm_status.json` 会以非零状态退出。报告仅输出来源字段白名单，不输出 TXT、原始输入或提示词。

## 验证落点

- `tests/unit/test_artifact_provenance.py`：确定性 envelope 指纹、字典键顺序稳定、文本/结构化输入变化、校对和总结的真实处理器参数。
- `tests/integration/test_artifact_provenance.py`：真实协调器与缓存写入流程生成两层；确认总结指纹使用校对输出和实际说话人数；验证姓名恢复后的最终文件字节；只重写一层时保留另一层记录；旧缓存、损坏 JSON、篡改文件、写入失败；通过 CLI 子进程查验实际文件。
