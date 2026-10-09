<!-- delegate-outcome: succeeded -->

# E 卡独立审查结论（r1）

failure-visibility: p2-only

## 结论

发现 3 项 P2 测试证据缺口，当前不建议将 PR 标记为 ready；修正后再复审这些契约断言。固定审查范围为 `0af9901639f7b4940c6a9d051f467f2a13a971d1..56ad49b4127b10953f92c6fb72443561e1c6ba95`，结论针对该范围，不代表生产容量、真实模型质量、恢复域或部署安全已验证。

## Findings

### P2：浏览器 producer metadata 未与 SQLite 持久化值比较

- **位置**：`e2e/tests/local-upload-full-chain.spec.ts:218-232`；`tests/local_upload_real_server.py:118-128`。
- **证据**：E2E 解码并断言浏览器发出的 `X-Upload-Metadata` 等于 fixture，但真实状态查询没有返回 `local_uploads.request_metadata` 或任务的 `processing_options`，随后也没有把它们与浏览器实际 metadata 比较。独立 synthetic HTTP 测试覆盖了服务端 metadata 路径，但不能证明此浏览器 producer 的 wire payload 被实际持久化。
- **影响**：浏览器功能或服务端解析若丢弃/改写处理选项，当前真实 producer 全链路仍可能通过。B3 的跨边界合同及合同索引所称“比较 producer bytes、SQLite metadata”没有被该 E2E 锁定。
- **最小修正**：让 E2E 状态查询返回实际存储的 request metadata 与处理选项，在真实浏览器上传用例中断言它们与解码后的 producer metadata 一致；同步修正合同索引描述。
- **交付阻断**：是。

### P2：真实全链路用例未观察意外 request failure 和 HTTP 错误响应

- **位置**：`e2e/tests/local-upload-full-chain.spec.ts:168-173, 371`。
- **证据**：用例只收集 `pageerror` 和 `console` 的 error 类型。它逐项检查了被显式等待的几个请求状态，但没有注册 `requestfailed` 观察器，也没有收集页面请求中未预期的 HTTP 4xx/5xx。测试末尾的 `browserErrors` 因而不约束这两类失败。
- **影响**：全链路仍可能在后台出现失败请求或意外 HTTP 错误而通过，不满足 E 卡对意外浏览器网络失败可见的验收不变量。
- **最小修正**：在此真实用例中记录页面 `requestfailed` 和非预期 HTTP 错误响应，并在结束时断言为空；对有意预期的状态作局部显式声明。
- **交付阻断**：是。

### P2：读取/到期测试没有控制在途读取跨越到期边界

- **位置**：`tests/integration/test_upload_races.py:65-102`；`docs/testing/local-upload-contracts.md:36`。
- **证据**：测试先把首个权限检查的 `now` 固定为截止前一秒，暂停后立即释放请求；之后再对一个全新 GET 固定使用截止时刻并断言 404。它验证了截止前授权的读取和截止时刻的新读取结果，但没有在首个请求暂停期间推进时间至到期后再放行，因此没有证明“已授权传输跨过到期仍可完成”的受控竞态。索引称其为“两种/受控顺序”的表述超出了当前断言。
- **影响**：若读取路径在授权之后、发送正文之前错误地重新按当前时刻拒绝，当前测试不能区分这种行为与合同允许的在途传输继续完成。设计合同明确允许已经授权的传输继续，并要求新读取在到期后拒绝。
- **最小修正**：在首个 public read 的授权后暂停时，将测试时钟推进到到期边界，再放行并断言该响应仍成功；随后以相同到期时间发起新 GET 并断言 404/无正文。索引准确描述所覆盖的顺序。
- **交付阻断**：是。

## B1–B16 契约索引核对

- **有具体测试映射且本地窄验证通过**：B1、B2、B4–B14、B16。B16 只验证本地开关关闭时的拒读与清理保护，不等于实际恢复域证明；合同文档对此限制有明确说明。
- **部分证据**：B3 的 producer 字节与 metadata wire header 断言存在，但实际浏览器 metadata 到 SQLite 的绑定缺口见 P2-1。B15 有 Chromium 提交、历史、读取与撤销路径，网络失败观察缺口见 P2-2。
- **B5**：文档映射指向真实 intake/dispatcher 测试，当前标准测试入口通过。
- **部署和生产义务**：生产额度/容量、外部镜像恢复域、生产 ASR/模型质量和生产启用未被本次本地证据覆盖，也不是本地 E2E 可替代的证明。

## 验证与证据

- 固定 HEAD：`56ad49b4127b10953f92c6fb72443561e1c6ba95`；基线：`0af9901639f7b4940c6a9d051f467f2a13a971d1`。审查前 worktree clean，差异为 8 个文件、1290 行新增、31 行删除。
- 官方 scratch 中执行 `TMPDIR=/tmp make test && npm run test:web && npm run test:browser`，退出码 0。浏览器测试 17/17，Web 测试 186 passed；pytest 入口完成且有既有 skip，静默输出没有打印最终通过计数。日志保存在本次审查 dispatch scratch 的 `narrow-verify.log`。
- 实际临时 systemd unit 在目标环境执行 `tests/unit/test_local_upload_policy.py`，17 passed，`Result=success`、`ExecMainStatus=0`；记录保存在 dispatch scratch 的日志中。
- 官方 scratch 重放了到期读取用例和浏览器真实全链路用例，分别 1/1 通过。真实 producer 契约产物与哈希、SQLite race 数据库及真实子进程环境记录保存在本次审查 dispatch scratch 的 `real-chain-evidence/`、`race-sqlite-evidence.db` 和 `real-server-runtime-evidence.txt`。本项为通过证据，不消除上列未锁定点。
- 变异检查：将 `CacheManager.local_upload_share_is_active` 的到期判断临时反转，真实 HTTP route race 用例按预期以 `404 == 200` 断言失败；恢复后源码哈希与原值一致，临时变更不在差异中。
- OCR 审查：一次执行返回 `status=skipped`，原因 `backup:deepseek=leg_timeout`；primary 返回 `content_error`。按纪律未重试，故 OCR 结论为 skipped，不视为软件 finding。
- Hosted CI 与 PR ready 后的完整主审未运行；审查没有改变 PR ready 状态。基线 CI 结论也未能通过当时的 GitHub API 查询确定，保持未知。
- 证据边界说明：审查中一次宽范围文本检索误触及 session progress 文档；其内容未作为判断依据。判断仅基于固定代码差异、批准设计合同、变更的契约索引及实际验证产物；未读取 PR 描述或其他审查记录。

## 详细执行报告

`report.md`（写入本次审查 dispatch 配置的独立报告目标）
