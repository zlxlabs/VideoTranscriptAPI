# 进度存档：章节生成 start_seg 乱序排序归一化 + chapters_error 落盘

Task-Id: VideoTranscriptAPI-20261015-01（dispatch dlg-20261010-101342-e71d15）

## 里程碑 1：A 排序归一化

- 当前阶段：repairing（A 完成并测试通过，B 未开工）
- 本段结论：`_validate_and_normalize_start_segs` 去重后非严格递增不再判 FAILED，改为按
  start_seg 排序归一化；排序确实改变顺序时打 warning（含被移动章节数 moved、LIS 长度，
  生产样本实测 moved 6/17、LIS 14/17）。排序发生在 `_derive_end_segs` 之前，区间推导
  自洽；已升序输入无排序、无新 warning，行为与 HEAD 一致。生产 attempt 2 样本序列
  （17 章）固化为测试输入 `tests/unit/test_chapters_processor.py::TestStartSegSortNormalization`。
- 关键决策与已否决方案：按卡面锁定决策执行（无条件排序、无 LIS 阈值、无碎章否决、
  prompt 不改、不新增端点）；统计口径选择「moved=逐位与排序结果比较的位移数」+
  LIS 长度双口径，与卡面「错位 3/17」（= 17 - LIS 口径）并存不冲突。
- 下一步唯一动作：实现 B（save_llm_status 增加 chapters_error 参数 + GENERATED 显式
  清空）并接线 llm_ops，配套 test_cache_manager / test_chapters_pipeline_wiring 测试。

## 里程碑 2：B chapters_error 落盘 + llm_ops 接线

- 当前阶段：repairing（A/B 实现与测试均完成，剩 CHANGELOG 与全量 make test）
- 本段结论：`save_llm_status` 新增 `chapters_error` 参数（None 不更新，非 None 写入）；
  合并逻辑新增「chapters_status=generated ⇒ 强制 pop chapters_error」不变式，集中在
  cache_manager 一处，caller 误传也无法制造「已生成却显示旧错误」。llm_ops 提取
  chapters_error（顶层优先、stats 兜底）传入 save_llm_status；未触碰层（None）保留旧值
  语义不变。契约测试 4 个（FAILED 落盘 / 补跑清除 / 误传仍清除 / 未触碰保留）+ 接线
  测试 3 个（FAILED 传递 / stats 兜底 / GENERATED 传 None）。
- 关键决策与已否决方案：GENERATED 清空放 save_llm_status 内部而非 caller 侧——不变式
  单点锁定，任何 caller 都无法绕过（锁定决策 4）；skipped_*/disabled 状态不清旧
  error（轴表只约束 GENERATED 清除，扩大清空范围属超卡面行为变更，报告里备注）。
- 下一步唯一动作：写 CHANGELOG Fixed 条目，跑 make test 全量门禁，写报告。

## 里程碑 3：收尾（CHANGELOG + 全量门禁）

- 当前阶段：验收中（A/B 已提交并各自红验通过，CHANGELOG 已写，全量门禁已过）
- 本段结论：CHANGELOG 新增 [Unreleased] Fixed 两条（排序归一化、chapters_error 落盘）。
  make test 全量门禁 exit=0（TMPDIR=/tmp；默认 TMP 下 test_unix_domain_socket_is_not_blocked
  因本 worktree 根路径 88 字符致 mkdtemp socket 路径超 AF_UNIX 108 字节限制而失败，
  该测试文件自 2026-10-02 d9f2e2c6 未变，属环境性继承红，与本次改动无关）。
- 关键决策与已否决方案：无
- 下一步唯一动作：写 report.md 交验收。
