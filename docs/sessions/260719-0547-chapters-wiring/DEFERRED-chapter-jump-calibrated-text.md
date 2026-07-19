# 延后：校对文本上的章节跳转（CW / YouTube plain 路径）

> 记录时间：2026-07-19  
> Session：`260719-0547-chapters-wiring`  
> 状态：**延后处理**（用户确认：先合分支，跳转增强另开任务）  
> 相关分支：`feat/chapters-wiring`（接线批）；设计 `docs/plans/2026-07-16-chapter-outline-design.md` v2.1.1

---

## 1. 问题现象

在查看页同时具备：

- 上方「章节梗概」卡片 + 右侧 TOC「章节梗概」组  
- 下方「校对文本」主阅读区  

时：

| 来源 | 章节能否生成 | 点目录/章节标题能否跳到正文 |
|------|----------------|------------------------------|
| FunASR（说话人 / structured） | 能 | **能**（`#dlg-{start_seg}` 命中 `id="dlg-i"`） |
| CapsWriter（不分说话人） | 能（有 timeline 侧车时） | **不能** |
| YouTube 平台字幕 | 能（有 cue segments 时） | **不能** |

真机复验（wiring worktree，8010）：

- CW 巫师 `BV18QLD6eEYz`：`chapter-cards>0`，`dlg_ids=0`，`data-jump-ok=0`  
- YouTube `Q8Fkpi18QXU`：同上  
- FunASR 小宇宙：`dlg_ids>0` 且 `href` 与 id 对齐  

为避免死链，T7 后逻辑为：无 structured dialog 锚点时 **不挂** `#dlg-*` 链接（`_page_has_dialog_anchors`）。因此 plain 路径表现为「目录不可跳 / 点了无响应」，而不是乱跳。

---

## 2. 根因（已定位）

### 2.1 章节分段逻辑（与跳转无关的部分）

1. **Timeline 小段**来自上游（ASR tokens/字幕 cue），不是章节 LLM 切的。  
2. 章节输入梯度：本轮 dialogs → 缓存 `llm_processed` dialogs → `load_segments()` 侧车 → 否则 `skipped_no_timeline`。  
3. LLM 只返回每章 **`start_seg`（原始列表下标）** + title/gist；`end_seg`/时间由代码推导。  
4. 成功写 `llm_chapters.json` + `chapters_status=generated`。

### 2.2 断点在「正文渲染坐标系」

| 层 | FunASR structured | CapsWriter / YouTube |
|----|-------------------|----------------------|
| 章节坐标 | `start_seg` → 列表下标 | 同左（侧车 segments） |
| 正文渲染策略 | `structured`：按 dialogs 输出块 | `capswriter_long_text`：整篇 `llm_calibrated.txt` → `<p>` |
| DOM 锚点 | `id="dlg-{i}"` | **无** |
| TOC/章节链接 | `#dlg-{start_seg}` | 无目标 → 禁用跳转 |

**一句话**：章节用 timeline 下标；plain 校对区是无下标的长文本流；只有 structured 路径在 DOM 上打了钉。

**不是**：章节生成坏了，也不是 TOC JS 单独坏了。

---

## 3. 产品约束（讨论结论）

- 用户希望 **优先阅读校对文本**（ASR 错字多已修）。  
- 精确跳转要求：用户正在看的 DOM 流里存在与 `start_seg` 对齐的节点，且节点内最好是校对字。  
- plain 路径今天只有 **整篇** `llm_calibrated.txt`，**没有** `seg[i] → 校对句` 映射。

因此：

| 目标 | 是否必须改 plain 校对 |
|------|------------------------|
| 只看校对 + **精确**跳到「该章对应校对段」 | **是**（plain 段级/structured 校对，近 T8） |
| 只看校对 + **大概**滚到进度附近 | 否（时间/字数比例虚锚点，仅渲染层） |
| 跳到 ASR 分段块、校对另区 | 否（渲染层；与「只看校对」冲突） |

**FunASR 说话人路径可基本不动**；缺口集中在 **CapsWriter + YouTube 不分说话人**。

---

## 4. 可选方向（延后选型，不在本接线批实现）

### 方向 A — 精确：plain 结构化/段级校对（推荐中长期）

- 对齐计划 **T8 / 阶段二**（`structured_calibration_for_plain` 等）。  
- 产物与 FunASR dialogs 同构或可映射到统一 `seg-{i}`。  
- 渲染主视图 = 段级校对块；章节/TOC → `#seg-{start_seg}`。  
- 成本：LLM/延迟/开关与回归。

### 方向 B — 大概：校对长文 + 时间/字数比例锚点

- 不改校对管线。  
- `ratio = start_time / duration`（或累计 ASR 字数加权）→ 校对流 `scrollTop` 或虚 `span`。  
- 体验：电子书式「跳到附近」；静音不均、校对大幅删字时会偏。

### 方向 C — 双区：分段 timeline（可跳）+ 校对全文（通读）

- 跳转 100% 准，但落点可能是 ASR 字。  
- 与「只看校对」不完全一致。

### 统一协议建议（任一方向落地时）

- DOM id 统一为 `seg-{i}`（或过渡期双写 `dlg`/`seg`）。  
- `jump_ok` 以「页面是否真有该锚点」为准，而非「是否 FunASR」。  
- 无锚点：TOC nolink + 可解释，禁止死链（保持现纪律）。

---

## 5. 本接线批已做 / 明确不包含

**已做（T1/T6/T7 等）**：timeline 侧车、章节生成与状态、章节卡片与 TOC XSS、无 structured 时禁用死链、功能文档与真机 e2e。

**本记录项明确延后**：

- [ ] 校对文本上的章节/TOC 精确或近似跳转（CW / YouTube）  
- [ ] plain 段级校对与锚点统一（若选方向 A，并入 T8 或新 session）  
- [ ] 虚锚点近似跳转（若选方向 B，可独立小任务）  

**本地 e2e 注意**：曾为演示将 worktree `config` 中 `min_chapters_threshold` 临时改为 `1000`，**勿当作生产默认合入**；合并/部署前恢复示例默认（10000）或仅保留在本地 config。

---

## 6. 建议的后续 session 入口

1. 产品拍板：A 精确 / B 大概 / C 双区。  
2. 若 A：从 T8 任务卡扩写「锚点契约 + 渲染主视图 = 段级校对」。  
3. 若 B：新开小任务「calibrated 虚锚点 + TOC 接 start_time」，不碰 processor。  
4. 真机验收：同一 BV 在 CW 路径点 TOC 必须落到可见正文并高亮。

---

## 7. 相关代码锚点（符号名，勿依赖行号）

| 主题 | 位置 |
|------|------|
| 渲染策略 | `utils/rendering/dialog_renderer.py`：`_get_optimal_rendering_strategy` / `_render_from_structured_data` / `_render_capswriter_long_text` |
| 章节 HTML / jump_ok | `render_chapters_html`；`api/routes/views.py`：`_prepare_success_view`、`_page_has_dialog_anchors` |
| TOC | `web/static/js/floating-toc.js`：`extractChapters`、`data-jump-ok` |
| 章节生成 | `llm/processors/chapters_processor.py`：`start_seg` 原始下标契约 |
| 输入梯度 | `llm/coordinator.py`、`api/services/llm_ops.py`：`timeline_segments` / chapters stage |

---

## 8. 修订历史

| 日期 | 说明 |
|------|------|
| 2026-07-19 | 初版：根因、产品约束、A/B/C 方向、延后声明；用户确认先合分支 |
