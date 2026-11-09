### 里程碑 1：身份绑定测试先红
- 当前阶段：TDD 红已验证，准备提交测试。
- 本段结论：在 Base `b54657b4dc422d5579735e13bbaf2332856496d9` 上运行新身份测试，13 个参数化用例失败、5 个通过、进程退出码 1；失败覆盖错位/空 batch 回退、空完成结果、未知状态、错任务/缺任务 ID 推送及上传完成阶段未校验身份。
- 关键决策与已否决方案：上传完成后复用已掌握的 upload_ready task_id 校验回执；初始缓存命中仍维持无 task_id 旧协议。
- 下一步唯一动作：提交测试红与本段进度。

### 里程碑 2：实现身份校验
- 当前阶段：实现完成，待提交与最终验证。
- 本段结论：轮询只接受本 task_id，显式 poll-miss 保留重投，完成空结果与未知状态 fail-fast；上传完成回执与 task_complete 推送加入身份校验，`FUNASR_PROTOCOL_ERROR` 使用现有 `FunASRFatal` 类型承载。源文件保持 802 行，移除 `items[0]` 回退。
- 关键决策与已否决方案：不增加异常类；用 `FunASRFatal` 表示协议违约，避免把错位响应送进 PollMiss 重投分支。
- 下一步唯一动作：提交实现、身份测试补充和本段进度。

### 里程碑 3：提交后验证
- 当前阶段：窄测与静态验收通过，待推送并开 draft PR。
- 本段结论：提交后窄测 `tests/unit/test_funasr_client_polling.py`、`tests/unit/test_funasr_client_identity.py`、`tests/unit/test_funasr_terms.py` 全绿；身份测试独立 Accept-Check 全绿，`items[0]` 搜索无命中；card scope check 与报告测试判据合同检查通过。`make test` 按 ci-standard-v1 留给远程 CI，本机未运行。
- 关键决策与已否决方案：未在本机执行 Verify-Command `make test`，避免违反 ci-standard-v1；不等待远程 CI 终态。
- 下一步唯一动作：提交验证进度后推送分支并创建 draft PR。
