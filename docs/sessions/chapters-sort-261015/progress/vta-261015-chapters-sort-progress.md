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
