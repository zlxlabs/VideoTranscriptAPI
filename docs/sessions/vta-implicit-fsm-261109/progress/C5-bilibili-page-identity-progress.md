## 里程碑 1：TDD 红测试

- 当前阶段：implementing，已在 Base commit `b54657b4dc422d5579735e13bbaf2332856496d9` 上添加测试并验证失败。
- 本段结论：锁定 URL 解析的 P2 身份、同/跨下载器实例下的分 P cid、分 P 越界错误，以及 BBDown 下载结果身份。红测命令 `uv run --frozen pytest -q tests/unit/test_url_parser.py tests/unit/test_bilibili_bbdown_supervisor.py tests/unit/test_bilibili_page_identity.py` 结果为 6 failed、84 passed；新增 URL 与身份用例分别因未补页身份、缓存串页、未拒绝越界而失败。
- 关键决策与已否决方案：遵循锁定格式，P1 保留 BV，后续分 P 使用 `<BV>_p<N>`；不触碰缓存表及 BaseDownloader。
- 下一步唯一动作：提交本段测试及进度记录为独立 `TDD红:` 提交。
