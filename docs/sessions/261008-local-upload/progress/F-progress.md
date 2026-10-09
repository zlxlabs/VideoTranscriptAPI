# F 进度

## 2026-10-09 · 首个运维门槛单元

- **当前阶段**：implementing；独立只读门槛单元已提交 `03828d3231003fc6ef3f91e52d772950e8b4f796`、推送并由 draft PR #208 承接。
- **本段结论**：`uv run --frozen pytest -q tests/unit/test_upload_ops.py` 4 passed；`scripts/ops/local_upload_check.py` 只读取明确传入的消费配置/数据目录与当前进程开关，不加载 dotenv、不打印密钥、不启用/部署/恢复；即使四限额、开关和代码祖先检查满足，也会以 `UPLOAD_ENABLE_BLOCKED` 阻止生产启用，因为容量、独立恢复域和实际入口尚未知。
- **关键决策与否决方案**：消费配置解析沿用真实 `load_and_validate_config`；env 文件仅从 tracked compose 声明识别来源，绝不 source；兼容检查以 Git commit ancestry 与已知 A guard-only 完整 SHA 为边界，不用版本字符串比较；本地检查结果不作为 enable 权限或伪造容量凭据。首轮测试因预期把四个 `null` 错判为 `missing` 有 AssertionError 红，修正断言后全绿；生产代码未变异。
- **下一步唯一动作**：运行本仓全量 `make test` 并记录最终提交/PR 状态。

## 2026-10-09 · 恢复消费者与容量探针

- **当前阶段**：implementing；真实 SQLite+正文 snapshot/restore 回归、A guard-only 真实源码消费者测试、受控容量探针和运维文档已提交 `7c6e7cf083dce3a59b42189a64fe085394c36101` 并推送 draft PR #208；窄测 11 passed。
- **本段结论**：fresh Resolver 子进程接收恢复前 DB 且外部 gate 明确 false，实际拒绝读；current cleaner 保留有效 never 正文/root。独立 subprocess 从真实 A guard-only commit `6af391d20edab8dfd8b320ce6b91d4d19e3260e2` 运行 resolver/cleaner，active share 可读、upload/blank legacy alias 不可读、cache/task 两个 cleaner 不删有效长成果；pre-A commit 由 ancestry 负例拒绝。clean-tree 容量探针完整 SHA=`d33808abad7ebbc21981da659e556810c8e7216b`：20s PCM s16le mono/48kHz 样本 1,920,044 bytes；2/2 raw HTTP receipts，服务端接收摘要一致；两客户端、URL下载与上传时间重叠确认，临时目录采样峰值 3,840,088 bytes、data 树峰值 4,572,561 bytes，10ms 采样。机器 x86_64/20 CPU/Python 3.11.15，ffmpeg/ffprobe 6.1.1。ASR loopback rejector 收到 4 次连接，三个 worker 均 failed 是预期；真实 ASR、模型最大时长、生产 ingress/代理、实际容量与恢复域仍 unknown。真实 preflight 用 config example、temp users/data、精确 true 进程 env 跑出 `UPLOAD_ENABLE_BLOCKED` 与 pre-A 不兼容结论，退出码 2；dotenv 未加载。
- **关键决策与否决方案**：恢复 fixture 只用 pytest `tmp_path`；还原撤销前 DB 会令 SQLite 内 `revoked_at` 回到 NULL，所以无独立撤销来源就不能再开；脚本仅触达 loopback 服务/下载/ASR fixture，worker 失败不作为容量凭据。移除 config example 中消费者 validator 不接受的 `funasr_spk_server.send_terms` 示例键；消费者默认值仍为 false，不修改运行时消费者。
- **下一步唯一动作**：完成全量回归和部署状态核验，随后交 lead 接 hosted gate / 独立审查。

## 2026-10-09 · 全量回归与本地边界确认

- **当前阶段**：implementing；实现代码在 `d33808abad7ebbc21981da659e556810c8e7216b` 已完成本地全量验证；生产启用仍 blocked，PR #208 保持 draft。
- **本段结论**：两次 `make test` 均 exit 0，`PYTEST_ADDOPTS=-rs make test` 明确为 3,820 passed、3 skipped、0 failed（仅 `test_capswriter_samples_total_contract.py:61` 因真实 preflight fixture 不可用）；新增窄测 11 passed。clean-tree容量探针与真实 preflight均在最终实现 SHA `d33808...` 上执行；容量记录为 synthetic observation，preflight在 gate=true但限额/恢复/ingress未知时退出2并打印 `UPLOAD_ENABLE_BLOCKED`。测试生成的 data/log/cache、`.pytest_cache`、175 MiB本地 `.venv` 与 Python caches 已按本树初始 absent 状态采样后清理，最终工作树 clean。
- **关键决策与否决方案**：继承红因派发时 `gh api` 基线不可用，标记“未能判定”；本卡开发期红均为修正后的断言/fixture/脚本契约错误，最终全量无新红。未执行真实 systemd、生产服务/ingress/磁盘/ASR/备份恢复；未知项不改写为 passed，不改 GOALS/M6生产检查，不启用开关。
- **下一步唯一动作**：把 draft PR #208 与验证/blocked条件交 Pi lead，等待其正式 CI、独立审查与后续授权。

## 2026-10-09 · F-R1 ancestry 错误态修复

- **当前阶段**：repairing；仅运维 CLI 的 Git ancestry 非零映射、实际 Git unit 回归及本进度记录有改动，未提交。
- **本段结论**：新测试先在 H0 `de3ff7b6671418ae7746649ef2167d5778c790ef` 官方 scratch 以真实 AssertionError 红：临时仓 HEAD 由 `git rev-parse HEAD` 取得，minimum SHA 对象缺失，raw `git merge-base --is-ancestor` 实返 128，旧 helper 错报 incompatible。H1 修复后 narrow 12 passed；scratch 中 `make test` 与 `PYTEST_ADDOPTS=-rs make test` 均 exit 0，后者 3821 passed/3 skipped/0 failed，skip 为真实 CapsWriter preflight fixture unavailable。
- **关键决策与否决方案**：严格映射 Git 返回码 `0→safe-lineage`、`1→incompatible`、其它查询错误→已有 `unknown`；保留未解析 source→unknown，既有真实 A safe-lineage 与 pre-A incompatible 对照不变。不增状态、catch、fallback、重试或包装层；测试用真实一次提交仓和真实 producer argv，不mock subprocess。
- **下一步唯一动作**：提交并推送这三处修复到原 PR #208，核对远端/clean 后交 lead。
