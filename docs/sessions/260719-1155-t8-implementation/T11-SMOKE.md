# T11 章节 UI 重设计 —— 真机端到端冒烟记录

> 日期：2026-07-20 ｜ 环境：本地 :8000 服务器（运行 feat/chapters-wiring 最新代码）
> 脚本：一次性 /tmp 脚本，不入库。

## 路径 1：PC 章节页（3 个真实样本）

样本：Terence（YouTube，12 章）、巫师（bilibili，5 章）、小宇宙杨攀（13 章）。

每个样本断言：

- HTTP 200；
- 章节数据岛（`<script type="application/json" id="chapters-data">`）存在，且内容无原始 `<`（已全量 `\u003c` 转义）；
- 数据岛 JSON 可解析，章数正确；
- 全部章节 `jump_ok=true`；
- 内嵌章节头数量正确，且每个都恰好位于对应 `dlg-{start_seg}` 之前；
- 旧卡片墙 DOM 完全移除。

**结果：全部 PASS。**

## 路径 2：无章节兜底页

样本：bilibili 历史任务（`chapters_status` 为空）。

断言：

- HTTP 200；
- 无章节数据岛；
- 无内嵌章节锚点；
- `floating-toc.js` 仍加载（旧大纲路径）。

**结果：全部 PASS。**

## 路径 3：静态资源

- `floating-toc.js`（33508B）：HTTP 200，含新逻辑标记（chapters-data 读取、chapter-sticky-bar、toc-wide-margin）；
- `floating-toc.css`（16870B）：HTTP 200，含对应新样式标记。

**结果：全部 PASS。**

## 局限（诚实标注）

- HTTP 层契约全部验证通过。
- JS 交互逻辑由 mini-DOM 线束验证（47 断言全过）。
- CSS 实际视觉效果（宽屏居中、暗色主题、gist 两行截断、触摸交互）**未在真实浏览器验证**，建议用户过目。
