# 分诊进度

## 2026-10-12 pickup → 源码核验

- **阶段**：planning / 证据采集。
- **本段结论**：独立 worktree `card/vta-261012-triage-audit` @ `c7dc7fcb`，无交接单。8 条 open 与卡面一致。PR234 不碰。
- **关键决策与已否决**：#191 不改时长上限；#227 不重提双指纹/内嵌锚点/新端点；#204 旧「接受不修」不等于无缺陷。
- **下一步唯一动作**：写完 triage 后跑仓外复现探针。

## 2026-10-12 探针

- **阶段**：planning / 复现。
- **本段结论**：#226 生产 writer 证实 FAILED→skipped_*/disabled 残留 error；#212 捕获未转义 title payload；#204 真实 handler 注入写库失败后 inflight/budget/槽/临时目录泄漏（HTTP 500）；#211 真 TCP uvicorn 打出 `h11.LocalProtocolError` ERROR 日志，GlitchTip 收据 unknown。#216/#227/#199/#191 以源码+git+评论为准，未跑 Playwright/全量 suite。
- **已否决**：把 204 首次 503 worker-not-ready 当成缺陷；把历史 4.8 倍率当校准样本；把 uvicorn ERROR 日志等同 GlitchTip 已收。
- **下一步唯一动作**：提交 triage.md 与本进度，再写 design.md。

## 2026-10-12 设计落盘

- **阶段**：planning / design。
- **本段结论**：四张 ready 增量 A#226 B#204 C#212 D#216；#227 关单；#211/#191/#199 取证或 blocked。design.md 五段已填。
- **已否决**：把 #211/#191/#199 塞进 ready 卡；为「根治」上新框架。
- **下一步唯一动作**：commit+push 本分支，写 delegate 报告。主脑确认后再派实现卡。

## 2026-10-12 关单验证（#211 复跑 + #227 窄验）

- **当前阶段**：planning / 收口核验（复核卡，不实施产品修复）。
- **本段结论**：#211 真TCP uvicorn 复跑成功：stderr 第5行 `ERROR:    Exception in ASGI application`、第25行 `h11._util.LocalProtocolError: Illegal header value b'text/plain\r\nX-Injected: 1'`；证据 `/tmp/vta-261012-closeout-211/probe-211-stderr-20261012T1012.log`（sha256 `46773ca201cca3e6813347ea6e9efc6c93a947121dff85ec68ba25cfb28a2e46`），脚本 `probe_211_uvicorn_rerun.py`（sha256 `352cf699044f3413320c9d197eb536f8d06dde2cd4cdb07e00fd24b950d5dabd`）；旧 `probe-211-uvicorn.json`（log_record_count=0）撤销作异常证据。#227 窄验：1596eca1 上使用项目 .venv 解释器+`PYTHONPATH=<卡树>/src` 跑 `pytest tests/unit/test_notes_anchor_source.py -q` exit 0、7/7，输出归档 `/tmp/vta-261012-closeout-227/pytest-notes-anchor.out`（sha256 `4b7ddc37fcc056878a4b2253828a2d3a98c92bc9ff8c3366e506c97891569e85`），主脑已回读确认已关单。#226/#204/#212 原探针命令+sha256+关键输出已固化进 triage.md。
- **关键决策与已否决**：不把 0 字节响应/旧 JSON 计数当异常证据；不可得即降 unknown（GlitchTip 收据仍 unknown）；不查 GitHub，标签按主脑核对改当前（226/227 已有 P2+本仓、216 实际补 bug 与建议 enhancement 分开）；不 uv sync、不覆盖 venv/shebang；#227 窄验绿仅支撑关单谓词，不声称产品全量验收。
- **下一步唯一动作**：落盘设计契约修订并 commit+push。

## 2026-10-12 设计契约收紧与脱敏定稿

- **当前阶段**：planning / design 契约收紧与公开脱敏定稿（只修方案不修产品）。
- **本段结论**：design.md 目标精炼为「未移交接收资源释放不被错误记录短路」；B 段以资源所有权为契约，明确写库失败 fail-fast 上抛不吞异常且覆盖 sqlite3.OperationalError 与 set_local_upload_error rowcount ValueError，相邻 cleanup 失败保证接收槽释放，排除顾问误判的 :273 槽泄漏，确切测试 tests/unit/test_upload_routes.py；C 段明确 sender 无法从拼好文本识别 title，改为格式组装时风控后按方言编码，覆盖 channel/wechat/router 三处，采用独立规格夹具断言真实 builder→sender 实收 payload 且期望值禁从被测 builder 计算，确切覆盖 3 个 src 与 test_wechat_notifier.py / test_feishu_channel.py；D 段明确 serverScratch 不得当短目录，构造一级短私有目录（按 AF_UNIX 108 字节预算），API 走 TCP 不改 Unix socket，测试 scope 包含 e2e spec 与 tests/local_upload_real_server.py；#211 保留白名单两行维持待取证，#191 明确历史 4.8 未核出处，#227 状态改为已关闭（主脑 GET 回读 CLOSED/COMPLETED）；四卡纲含测试预算且耗时均标 unknown，未来实现列出行数闸核对要求。全文脱敏删除本机用户名与 /home 绝对路径，探针命令使用相对说明，sha256 均给 64 位完整哈希。
- **关键决策与已否决**：严格遵守 public-scan 要求，不绕过扫描，不引入被拒 a025 为祖先；只改 3 份 scope 文档；否决把 #211 复跑写成「已证明 GlitchTip 收事件」；否决给 D 换 Unix socket 服务器；否决为「根治」扩产品范围。
- **下一步唯一动作**：commit+push 本分支，写 delegate 报告。
