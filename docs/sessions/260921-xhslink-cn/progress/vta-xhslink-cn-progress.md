# vta-xhslink-cn 进度存档

## 段落 1 — 域名补全 + UA 常量（commit 416b062）

- 当前阶段：implementing，源码改动完成
- 本段结论：七处源码补上 `xhslink.cn` 识别；短链展开 HEAD/GET 统一带移动端 UA，常量只定义在 `utils/url_parser.py` 一处、`downloaders/base.py` import 复用。
- 关键决策与已否决方案：不新增 `xhslink\.cn` 正则（防 `/o/` 误捕获与串缓存）；登录墙不做识别（400 透传）；沿用"失败沿用原始 URL"语义。
- 下一步唯一动作：补测试用例。

## 段落 2 — 测试覆盖（commit cb91b0d）

- 当前阶段：implementing，测试补齐
- 本段结论：生产 302 Location 原文作为契约 fixture 入 `test_url_parser.py`；UA 断言覆盖 `url_parser` 与 `base` 两条路径；`can_handle` / factory 路由 / `_clean_url` / 平台识别各加 `xhslink.cn` 用例。窄范围 302 项全绿。
- 关键决策与已否决方案：无。
- 下一步唯一动作：红验 + 全量收尾。

## 段落 3 — 红验与全量收尾

- 当前阶段：implementing，待收尾提交
- 本段结论：两次最小注入红验均按预期以 AssertionError 转红（media_resolver 域名行、url_parser HEAD UA 行），还原后 worktree 与对应提交逐字节一致；`uv run --extra dev pytest tests/unit -q` 全量 2875 passed。
- 关键决策与已否决方案：红验中发现 python 文本重写会把 `url_parser.py` 的 CRLF 行尾转为 LF，已用二进制方式转回并经 `grep -c $'\r'` 逐文件核对行尾无损。教训：CRLF 文件一律二进制改写。
- 下一步唯一动作：提交本文件并写报告。

## 段落 4 — 轮 1 R1：UA 收窄到小红书短链域名

- 当前阶段：implementing，R1 修复完成
- 本段结论：新增 `short_url_headers()`（`url_parser.py` 唯一值源，`base.py` 复用），仅 `xhslink.com` / `xhslink.cn` 带移动 UA，其余域名传 `headers=None` 与加 UA 前逐字一致；回归锁覆盖 xhslink 双域名带 UA 与 `v.douyin.com` 不带 UA。窄范围绿。
- 关键决策与已否决方案：无差别加 UA 会触发抖音按 UA 分流到 `iesdouyin.com`（resolver 400），故按锁定修法做域名白名单而非全局 UA。
- 下一步唯一动作：R1 红验后做 R2+R3。

## 段落 5 — 轮 1 R2+R3：前端/文档补齐与恒真用例删除

- 当前阶段：implementing，R2+R3 完成
- 本段结论：`app.js` 的 `videoDomains` 与标题 hostname 分支各补 `xhslink.cn`（grep 命中 2 处，无 JS 测试基建）；`media_resolver.md` 链接形态枚举加一行；删掉镜像逻辑的恒真用例 `test_xiaohongshu_cn_short_link`（词表对齐保留）。
- 关键决策与已否决方案：无。
- 下一步唯一动作：全量收尾并写报告。
