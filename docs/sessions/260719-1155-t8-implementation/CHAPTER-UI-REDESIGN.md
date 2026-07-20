# 章节 UI 重设计实施规格（T11）

日期：2026-07-19 ｜ 分支：feat/chapters-wiring ｜ 状态：已完成（含用户验收迭代 4 轮）

## 背景与目标

T8/T9 后章节以「正文卡片墙」形式上线（每张卡：编号+标题+时间+摘要全文，12~13 章占近
两屏），目录边栏把章节与「内容总结」大纲标题混在一棵树里。用户认定不合理，参考
飞书妙记（时间轴速览+原文内嵌章节头）与通义听悟（常驻章节侧栏）重设计。

核心诉求（用户原话）：通过章节速览了解不同章节内容，然后快速点击、跳转、切换；
**必须保证手机端和 PC 端阅读体验**。

设计共识（已与用户确认）：
- 章节是「索引层」不是正文：速览常驻可达，摘要克制（默认两行截断）。
- A2 方案：升级现有浮动目录为「章节速览面板」，不做真两栏。
- 顶部卡片墙**彻底移除**，章节完全归入面板/抽屉 + 逐字稿内嵌章节头。
- 摘要默认两行截断、点击展开全文。

## 风险定级

自用工具、分享给少数朋友、生成内容默认暴露公网。
→ 章节 title/gist 来自 LLM 输出，**必须防 XSS**：JSON 数据岛转义 `</script>`，
前端一律 textContent/安全插入，禁止 innerHTML 拼接未转义内容。

## 现状关键位置（探查结论）

- 查看页模板：`src/web/templates/transcript.html`，章节区块 `:224-233`
  （`{% if chapters_html %}` → `{{ chapters_html|safe }}`）。
- 卡片渲染：`src/video_transcript_api/utils/rendering/dialog_renderer.py:661-758`
  `render_chapters_html()`（卡片墙，gist 全文直出）。
- 章节数据加载与门控：`src/video_transcript_api/api/views.py:974-1125`
  （读 `llm_chapters.json`、重算指纹、`_page_has_dialog_anchors()` 判跳转能力，
  `:1115-1118` 调 render_chapters_html）。
- 逐字稿结构化渲染：`dialog_renderer.py:401-491` `_render_from_structured_data()`，
  每段 `<div id="dlg-{dlg_index}" class="dialog-item" data-start-time="...">`。
- 浮动目录：`src/web/static/js/floating-toc.js`（IIFE 原生 JS，无库）+
  `src/web/static/css/floating-toc.css`。
  - `extractChapters()` `:168-199` 扫 `.chapter-card`；`buildTocList()` `:203-254`
    把大纲树+章节组+校对文本拼进同一 `<ul>`。
  - 跳转 `handleTocClick()` `:351-383`（scrollIntoView smooth）；高亮
    `setupScrollObserver()` `:447-478`（IntersectionObserver）。
  - PC fixed 右侧面板默认收成指示条，pin 状态 localStorage `:54-70`；
    移动端 <768px FAB+底部抽屉（`floating-toc.css:319-389`）。
- 布局：`base.html:50-58` body max-width 900px 单列；章节卡片样式
  `base.html:989-1077`；无任何 `<audio>/<video>` 播放器。
- 章节数据契约（`llm_chapters.json`）：`chapters[] = {index,title,gist,start_seg,
  end_seg,start_time,end_time}`，见 `cache/cache_manager.py:1381-1394`。

## 目标设计

### 断点行为

| 屏宽 | 章节入口 | 形态 |
|---|---|---|
| ≥1400px | 常驻展开面板 | body 让出右边距，面板占位不遮挡正文 |
| 768~1399px | 浮层面板 | 覆盖式，默认展开，可手动收起（偏好存 localStorage） |
| <768px | 吸顶当前章条 + FAB + 底部抽屉 | 点条目跳转后抽屉自动收起 |

### 组件清单与 DOM 契约

1. **章节数据岛**（替代卡片墙）
   - views.py 把 chapters 列表序列化为 JSON 注入模板：
     `<script type="application/json" id="chapters-data">`。
   - 每章字段：`{index,title,gist,start_time,start_seg,jump_ok}`（jump_ok 沿用现有
     指纹+锚点门控结论；false 时条目不可跳转，视觉降级）。
   - 序列化必须转义 `</` → `<\/`（防 `</script>` 逃逸），模板层不再 `|safe` HTML。
   - 原 `chapters_html` 变量与 `transcript.html:224-233` 章节区块删除。

2. **逐字稿内嵌章节头**（dialog_renderer）
   - `_render_from_structured_data()` 渲染到 `dlg_index == 某章 start_seg` 的段落前，
     插入：
     `<div class="chapter-anchor" id="chapter-anchor-{index}" data-chapter-index="{index}">
        <span class="chapter-anchor-time">{mm:ss}</span>
        <span class="chapter-anchor-title">{title}</span></div>`
   - title 服务端转义；仅当该章 jump_ok 时插入（无锚点能力的页面不插）。
   - 需要把 chapters 数据传入逐字稿渲染路径（views.py 渲染顺序若章节晚于逐字稿，
     调整为先读章节再渲染逐字稿，或逐字稿渲染后做 DOM 级插入——实现者选侵入小的）。
   - 纯文本路径（无 dlg 锚点）不插入，行为不变。

3. **章节速览面板**（floating-toc.js/css rework）
   - 面板顶部加切换：`章节 | 大纲`；有章节时默认停在「章节」，无章节时退化为现状
     （只显示大纲树，不显示 tab）。
   - 章节 tab 条目：`<button class="toc-chapter-item">` 含
     `.toc-chapter-time`（mm:ss）+ `.toc-chapter-title` + `.toc-chapter-gist`
     （CSS `-webkit-line-clamp: 2` 截断，点击条目展开/收起 gist；
     跳转通过条目上的独立跳转区域或标题点击——交互二选一，实现后评审）。
   - 跳转目标：`dlg-{start_seg}`（与章节头相邻，scrollIntoView smooth）。
   - 当前章跟随：IntersectionObserver 观察内嵌 `.chapter-anchor`，高亮面板对应条目
     （`.current`），并自动滚动面板使当前章条目可见。
   - 大纲 tab = 现有标题树（移除其中的章节分组与「校对文本」改为大纲树普通节点即可，
     行为不变）。
   - PC ≥1400px：body 加右边距（如 320px），面板常驻展开不遮挡；768~1399px：浮层
     覆盖、默认展开、可收起，状态存 localStorage（新 key，不沿用旧指示条状态）。
   - 条目文本一律 textContent 注入（数据来自 JSON 数据岛）。

4. **移动端章节抽屉 + 吸顶当前章条**（<768px）
   - 抽屉内容 = 章节速览列表（同面板条目组件）；打开时自动滚动到当前章；
     点条目 → smooth scroll 跳转 → **自动收起抽屉**。
   - 吸顶条：滚动进入逐字稿区域后顶部吸顶显示当前章标题
     （`{index}. {title}`），点击拉开抽屉；滚出逐字稿区域隐藏。
     DOM：`<div class="chapter-sticky-bar" hidden>`，JS 控制显隐与文案。
   - FAB 保留（开抽屉入口之一）。

### 删除清单

- `dialog_renderer.py` `render_chapters_html()` 卡片渲染（被数据岛+内嵌头取代）。
- `transcript.html:224-233` 章节区块。
- `base.html:989-1077` 章节卡片样式（替换为内嵌章节头样式）。
- floating-toc.js 中 `extractChapters()` 扫 DOM 卡片的逻辑（改读数据岛）。

### 兼容性

- 无章节 / 章节生成失败 / 纯文本无锚点：面板退化为现有大纲目录，页面无章节痕迹，
  与现状一致。
- 旧 `llm_chapters.json` 数据契约不变，后端不动生成链路。

## 实施分阶段

- **阶段 1（后端/渲染，TDD）**：数据岛 + 内嵌章节头 + 拆卡片墙 + 单测更新。
  涉及：views.py、dialog_renderer.py、transcript.html、base.html（卡片样式→章节头
  样式）、tests/unit 相关测试。
- **阶段 2（前端交互）**：面板 tab/条目/跟随、断点行为、移动抽屉+吸顶条。
  涉及：floating-toc.js、floating-toc.css、base.html（少量）、transcript.html（挂点）。
- **阶段 3**：独立 subagent review 循环（gate=连续 2 轮无新增 P1，上限 20 轮）。
- **阶段 4**：真机端到端冒烟（本地服务器 + 三个真实样本 view_token）。
- **阶段 5**：文档同步（docs/features/chapters.md、processing_options.md 涉及 UI 的
  描述、AGENTS.md 如有约定变化）。

## 验收标准

1. review gate 通过（连续 2 轮无新增 P1）。
2. `uv run pytest tests/unit` 全绿。
3. 真机冒烟（详见阶段 4 执行记录）：
   - PC 路径：查看页含数据岛+内嵌章节头；面板章节条目可跳转。
   - 移动路径：<768px 吸顶条出现、抽屉跳转后自动收起（可用窄视口模拟）。
   - 兜底路径：无章节/指纹不匹配页面无任何章节元素，大纲目录正常。

## 实施记录（2026-07-20）

- **代码提交**：`43da8b3`（后端：章节数据岛 + 逐字稿内嵌章节头，移除卡片墙）、
  `fe5fdfb`（前端：章节速览面板 / 移动端吸顶条 + 底部抽屉）、
  `00d1191`（review 轮 1 修复：数据岛全量 `<`→`\u003c` 转义、宽屏正文在面板左侧
  剩余空间居中、start_time `math.isfinite` 守卫）。
- **Review gate**：独立评审 2 轮（R1 修复 P2×2 + P3×1，R2 复验修复并换角度新扫），
  连续 2 轮无新增 P1，gate 通过；接受不修的 P3 见 `REVIEW-LOG.md` T11 backlog。
- **真机冒烟**：PC 章节页（3 个真实样本）/ 无章节兜底页 / 静态资源三条路径全部
  PASS，详见 `T11-SMOKE.md`。
- **未浏览器验证项**：~~CSS 实际视觉效果（宽屏居中、暗色主题、gist 两行截断、触摸
  交互）未在真实浏览器验证，建议人工过目。~~ → 已由下方验收迭代 3 的 playwright
  截图验证闭环。

## 用户验收迭代（2026-07-20，gate 与冒烟之后）

真机体验驱动，每轮一个 commit。第 1、2 轮是用户对「摘要该放哪、放多少」的判断
摇摆，如实记录为探索过程。

1. `838c1b6` **章节头内嵌完整摘要，面板条目瘦身纯导航**——用户要求内嵌章节头补
   Description（时间+标题后追加完整摘要段，`html.escape`，空 gist 不渲染）；
   同轮误判「摘要都很短」，把面板条目 gist 一并删除，条目只剩时间+标题。
2. `282eb65` **面板章节条目恢复完整摘要展示**——用户推翻上轮：gist 本身很短，
   之前体验差的根源是「两行截断成残句」而非有文字。面板恢复完整 gist
   （纯 div，无截断、无展开/收起交互），标题保留单行省略。
3. `9bdce86` **章节面板排版升级为文档密度**——长摘要样本（小宇宙 13 章）gist
   实为 3~4 句段落，~335px 窄栏全量堆出文字墙。纯 CSS：docked 300→380px、
   wide-margin 预留 320→420px、mid 浮层 280→360px、抽屉 60vh→70vh、条目间
   hairline 分隔、时间/标题/摘要三级层次、gist line-height 1.7、当前章
   强调条+背景块。经无头 Chromium（playwright 装于 .venv，未入依赖清单）
   两轮截图迭代验证：三断点 + 双主题 + Terence 短摘要样本均过目。
4. `73e6605` **章节条目整条区域可点跳转**——gist 区域并入同一点击委托
   （`.toc-chapter-main, .toc-chapter-gist` → 路由到条目共享 main 按钮的 jump
   dataset），jump_ok=false 条目保持惰性；playwright 真实点击验证 PC docked
   与移动抽屉两条路径。

**最终形态**：内嵌章节头（时间 + 标题 + 完整摘要）+ 面板/抽屉条目（时间 +
标题 + 完整摘要、整条可点、当前章强调条高亮）+ 移动端吸顶条。原设计共识中
「摘要克制、两行截断、点击展开」的面板交互被迭代 2/3 推翻，以本节为准。
