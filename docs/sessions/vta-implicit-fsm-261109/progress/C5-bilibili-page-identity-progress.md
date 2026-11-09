## 里程碑 1：TDD 红测试

- 当前阶段：implementing，已在 Base commit `b54657b4dc422d5579735e13bbaf2332856496d9` 上添加测试并验证失败。
- 本段结论：锁定 URL 解析的 P2 身份、同/跨下载器实例下的分 P cid、分 P 越界错误，以及 BBDown 下载结果身份。红测命令 `uv run --frozen pytest -q tests/unit/test_url_parser.py tests/unit/test_bilibili_bbdown_supervisor.py tests/unit/test_bilibili_page_identity.py` 结果为 6 failed、84 passed；新增 URL 与身份用例分别因未补页身份、缓存串页、未拒绝越界而失败。
- 关键决策与已否决方案：遵循锁定格式，P1 保留 BV，后续分 P 使用 `<BV>_p<N>`；不触碰缓存表及 BaseDownloader。
- 下一步唯一动作：提交本段测试及进度记录为独立 `TDD红:` 提交。

## 里程碑 2：补齐无分 P 列表红测

- 当前阶段：implementing，继续补齐 TikHub 返回无 pages 列表但请求 P2 的锁定失败行为。
- 本段结论：新增 `test_tikhub_later_page_without_page_list_fails`；在独立 worktree 的 Base commit 上实跑该单测，结果为 1 failed，原因是旧实现未拒绝 P2 并使用 root cid。
- 关键决策与已否决方案：无；测试明确覆盖“无分 P 列表”而非只用超出已有列表的页码代替。
- 下一步唯一动作：提交该补充红测及本段进度，仍保持实现未提交。

## 里程碑 3：锁定分 P 元数据身份

- 当前阶段：implementing，补充对分 P 元数据 ID 与官方 API BV 参数分离的断言。
- 本段结论：扩展 BBDown 元数据测试，要求 P2 元数据仍使用裸 BV 请求官方 API、但返回 `BV_p2`；独立 Base worktree 实跑该测试为 1 failed，旧代码返回裸 BV 身份。
- 关键决策与已否决方案：官方 API 仍以 BV 查询视频级元数据，不把带 `_pN` 的缓存身份发给官方接口。
- 下一步唯一动作：提交本段红测试及进度记录。

## 里程碑 4：实现分 P 身份隔离

- 当前阶段：implementing，代码已完成；窄测在提交前通过，最终提交后仍需按卡面再验证。
- 本段结论：URL 解析与下载器统一生成 BV / BV_pN 身份，实例缓存按身份隔离；TikHub 按 p 选 cid，越界或缺页列表的 P2 抛出 `BILIBILI_PAGE_NOT_FOUND`，BBDown 返回同一身份。`bilibili.py` 保持 924 行，满足零余量文件大小闸。
- 关键决策与已否决方案：保留官方 API 使用裸 BV 查询；删除未使用的 `safe_title` 及官方元数据重试循环不可达的静默返回，循环意外落出时 fail fast，不新增缓存抽象。
- 下一步唯一动作：提交实现、绿测覆盖及本段进度记录。
