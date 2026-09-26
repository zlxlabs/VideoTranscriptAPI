# 校对与摘要产物来源审查

failure-visibility: clean

- 风险等级：personal。
- 冻结范围：`b1227035e6b34a01c6f49b4c71cdaf4ebd6fed07..9f3048c80f237f8a3341c199486cbe9ba110d580`。
- 独立只读审查：state_architecture；主脑复核：Codex。未发现违反本卡不变式的问题。
- 实际处理器输入在调用前冻结，摘要绑定真实校对输出；输入指纹经结果传入保存处。
- 输出指纹在姓名恢复及TXT保存后计算，来源记录走既有媒体锁及原子状态写入。
- 未改写层保留；旧调用方无新来源重写时清除该层旧记录。
- CLI只读单个媒体目录；旧记录unknown、文件缺失/篡改mismatch、坏JSON非零。
- `requested_model`只表示请求配置，实际模型及提示词版本仍为unknown。
- 验证落点：`tests/unit/test_artifact_provenance.py`、`tests/integration/test_artifact_provenance.py`；真实producer→文件→CLI及基线反向转红，117项定向/关联测试通过。完整回归和正式CI由最终PR记录提供。

## 兼容修复增量

- 固定范围：`9f3048c80f237f8a3341c199486cbe9ba110d580..13ec7b6c5dcd5fb22915f202e70ed7673d3ee42e`，独立state_architecture与主脑复核均clean。
- OCR发现并由原并发测试复现：legacy无来源时新增缓存读取会导致保存成功任务失败。修复将读取移入有效来源校验之后，旧调用仍保存状态并清旧来源，有来源但缺文件继续上抛。
- 四格`has_source × file_exists`回归断言真实状态落盘/缺文件异常与哈希；disabled标签有直接断言。未添加缓存状态、fallback或第二保存路径。
- 删除integration重复summary参数捕获后，unit仍核对真实summary调用与输入指纹，核心契约不变。修复后122项定向与关联测试通过，原并发用例通过。 A 合并后执行 `uv run --extra dev pytest -q tests/unit tests/integration`，全量测试命令退出码为0，收集3115项。
