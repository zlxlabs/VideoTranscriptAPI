# C3-calibration-status 进度存档（dlg-20261109-060447-247478）

## 段 1：TDD 红就绪

- **当前阶段**：implementing（TDD 红提交）
- **本段结论**：读完四处旧公式与全部相关测试，派生函数定稿为
  `derive_calibration_status(total_units, llm_output_units, degraded_units, partial_units)`
  加一个 dict 适配 `derive_calibration_status_from_stats`（第二消费者：llm_ops 通知 + views legacy）。
  新增 tests/unit/test_calibration_status_derivation.py（真值表 + 四调用点真实调用方断言），
  并按卡面有意契约改写 tests/unit/test_calibration_stats.py :212-218（低覆盖 → PARTIAL）。
  在 base 上新测试文件 import 失败、契约改写断言不匹配，均为红。
- **关键决策与已否决方案**：
  - llm_output_units 用「total - degraded」而非「success + partial」：前者在 legacy 数据
    缺 success/partial_count 字段时仍可计算，且让不在修改边界内的
    tests/unit/test_views_calibration_stats.py（TestDeriveLegacyCalibrationStatus 三条断言）
    无需改动即继续成立；「corrections 全空」由显式 dialog_counts.applied==0 信号纠正为 0。
  - 否决「通知构造在顶层 calibration_status 缺失时返回空串」：会让
    test_llm_ops_helpers.py 里 8 个只喂 calibration_stats 的既有测试全挂，而该文件被
    file-size 闸锁零余量（净增 ≤ 0，不能给测试数据补字段）；改为缺失时走 from_stats
    统一派生，既有测试全部自然成立。
  - 否决给结构化路径单独传布尔 has_llm_output：卡面要求入参为计数，applied 信号折叠进
    llm_output_units（==0 即无任何单元获得有效修正）。
- **下一步唯一动作**：跑窄测确认红（collection error + PARTIAL 断言失败），提交「TDD红:」。

## 段 2：实现转绿

- **当前阶段**：implementing（实现提交）
- **本段结论**：`utils/llm_status.py` 新增 `derive_calibration_status`（唯一口径）与
  `derive_calibration_status_from_stats`（dict 适配）。四个调用点全部改为调用：
  speaker_aware（applied==0 → llm_output_units=0，低覆盖压 PARTIAL）、plain_text、
  views `_derive_legacy_calibration_status`（变一行委托）、llm_ops `_build_calibration_warning`
  （DISABLED 先判；顶层 status 缺失时走 from_stats；NONE/PARTIAL 分别出完全失败/部分异常
  文案，PARTIAL 详情按 chunk/segment 字段展示计数并新增「覆盖率不足」子句）。
  窄测 5 文件 + test_views_calibration_stats + notification 相关全绿；`make test` exit=0。
  修复过程中改了自己红测里一处断言笔误（partial-only 场景 success=0，文案是 0/2 不是 2/2）。
- **关键决策与已否决方案**：
  - 通知构造不再维护独立公式，但 PARTIAL 的详情计数展示保留（测试断言 "3/5" 等锁住），
    chunk 路径详情新增 partial（覆盖率不足）子句——旧公式 partial-only 时静默无警告。
  - llm_ops 顶层 status 缺失分支保留：真实链路 coordinator 必写顶层 status，该分支只服务
    旧缓存/测试 mock 数据，且让 test_llm_ops_helpers.py 零余量约束下无需改测试数据。
- **下一步唯一动作**：跑全仓 `CalibrationStatus.FULL` grep 清单与 scope check，收尾报告与 draft PR。
