# 长逐字稿章节梗概（chapters）功能设计 v2.1

- **日期**：2026-07-16（v2.1：吸收 Codex 外部复核 14 条发现 + 解耦结构）
- **状态**：**安全批已完成并通过 Codex gate**（30 轮，R29+R30 连续两轮无实质新意见）；接线批等待 pr3 合入 main 后开工
- **分支**：`feat/chapters-foundation`（基于 main@ee092d1，72 commits，24 文件 +8438/-213，1860 单测全绿，未 push）
- **依据**：三份代码调研 + 关键路径逐行核验 + Codex 独立复核，所有结论以现有代码为准

## 版本变更记录

- **v1 → v2**：存量任务忽略（D4）；路线从"按来源分期"改为"先地基后功能"；校对拍板方案甲（D5）。
- **v2 → v2.1**（Codex 复核后）：**章节与校对切换解耦**——章节只依赖 timeline 数据层，阶段二变为带开关的独立升级；实施顺序调整为 **阶段一 → 阶段三 → 阶段二**；恢复 chapters-only 补层（失败可恢复）；章节输出 schema 改为 starts-only；跳转改直接 seg 锚点；json_object 模式替代 json_schema；`has_speaker` 模式贯穿全链路；XSS 修复扩展到现有 TOC；其余见 §13 处置表。

---

## 1. 背景与目标

7-8 万字长逐字稿没法通读。目标体验（精听目录式）：**章节 = 时间范围 + 标题 + 两三句梗概 + 点击跳转到校对文本对应位置**。

**非目标**：存量任务专门的回填工程（D4；FunASR 老任务因已有 `llm_processed.json` 顺带可补，CapsWriter/YouTube 老缓存无 segments 文件，诚实标记跳过）；播放器联动；总结"主题详述"瘦身。

## 2. 已拍板决策

| # | 决策 | 选择 | 理由 |
|---|------|------|------|
| D1 | 锚点体系 | 时间戳为主（展示语义）+ seg 索引直接锚点（跳转机制，v2.1 修订） | 时间范围展示直观；跳转用 `#dlg-{start_seg}` 直达，避免整秒时间碰撞的扫描歧义 |
| D2 | 章节 LLM 策略 | 单次直出 JSON（json_object 模式 + Self-Correction，v2.1 修订） | json_schema 模式失败即返回无重试（`llm.py:447-469` 已核验），json_object 才有 Self-Correction |
| D3 | 展示形态 | 独立区块 + TOC 挂标题 | 信息密度与导航兼顾 |
| D4 | 存量任务 | 不做专门回填工程 | 新任务全支持；chapters-only 补层恢复后 FunASR 老任务顺带可补 |
| D5 | 校对与时间 | 方案甲（逐段校对全统一）为目标架构，**带配置开关独立发布** | 时间"全程不丢"优于事后对齐；开关隔离回归面（Codex 根本性质疑的解法） |
| D6 | 实施顺序 | 阶段一 → 阶段三 → 阶段二 | 章节尽早上线（FunASR 跳转即刻精准）；校对切换风险独立灰度 |

**默认处理**：`min_chapters_threshold` 默认 10000 字符；继承共享缓存目录约束。

## 3. 总体结构（v2.1）

```
 阶段一：统一 timeline 数据层（地基，无悔工程）
┌────────────────────────────────────────────────┐
│ FunASR    → transcript_funasr.json（已有）       │
│ CapsWriter→ 接通 extra_json_data 落盘（断链修复） │
│ YouTube   → SubtitleResult DTO，三处解析保留时间  │
│ get_cache → 读回 segments JSON（当前不读，必修）  │
│ load_segments() 适配器：双命名兼容/文本永不丢     │
└──────────────────────┬─────────────────────────┘
            timeline: segments[{start,end,text,speaker?}]
                       │
        ┌──────────────┴────────────────┐
        ▼（不依赖阶段二）                 ▼（独立升级，配置开关）
 阶段三：章节生成+展示            阶段二：逐段校对推广（方案甲）
 输入梯度：本轮校对 dialogs       has_speaker 模式贯穿全链路
  → llm_processed.json           （coerce/推断/合并/文本/渲染）
  → 原始 segments                开关开：plain 源时间块渲染
 → 都无：SKIPPED_NO_TIMELINE          + 章节跳转精准锚点
 失败可恢复（补层重跑）           开关关：完全现状行为
```

跳转精度矩阵：FunASR 任务始终精准（`#dlg-{start_seg}`）；CapsWriter/YouTube 任务在阶段二开关开启后精准，关闭时章节区块正常展示（时间范围+梗概）但不带跳转链接。

## 4. 阶段一：统一 timeline 数据层

**验收标准（v2.1 措辞）**：三种来源的新任务**尽力**落一份统一契约的带时间 segments JSON；上游数据缺失（如 CapsWriter tokens/timestamps 缺失，`capswriter_client.py:686` 仅 warning 的 best-effort 行为）时不阻断任务，诚实降级（章节 `SKIPPED_NO_TIMELINE`）。

契约：`segments: [{"start_time": 秒(float)|None, "end_time": 秒(float)|None, "text": str, "speaker": str|缺省}]`

| 来源 | 改动 |
|------|------|
| FunASR | 零开发。**动手前实测字段名**（文档 `start_time/end_time` vs 样例 `start/end`），适配器兜底 |
| CapsWriter | `transcription.py` CapsWriter 分支把 `funasr_json_data` 传入 `save_cache(extra_json_data=…)`（机制已存在，写 `transcript_capswriter.json`）；核对重建 JSON 字段命名 |
| YouTube | 定义 `SubtitleResult` DTO（`{text: str, segments: list|None}`），改造 `get_subtitle()` / `fetch_transcript()` / `fetch_for_transcription()` 及全部调用方；三处解析保留时间（`youtube.py:558` / `youtube.py:693-717` / `youtube_api_client.py:583-589`）；segments 为 None 时行为与现状完全一致 |
| **读回（Codex #3）** | `get_cache()` 新增读回 `transcript_capswriter.json` → `cache_data["segments_data"]`（`cache_manager.py:539-556` 现只读 funasr json 和 capswriter txt）。修复"转录完成后崩溃 → 重提任务退回纯文本路径"的恢复窟窿 |

**`load_segments()` 适配器**：字段双命名兼容、类型规整（秒 float）、**文本永不丢**（Codex #12）——个别段时间坏/缺 → 保留文本、时间置 None（该段不作章节边界候选与锚点，正文照常校对展示）。

## 5. 阶段三：章节生成 + 展示（先于阶段二实施）

### 5.1 输入与依赖

章节**只依赖 timeline，不依赖校对**。输入梯度（llm_ops 层解析，经 cache_manager，保持 llm/ 包纯净）：

1. 本轮结构化校对产出的内存 dialogs（质量最优）
2. 缓存 `llm_processed.json` 的 dialogs（chapters-only 补层、FunASR 老任务）
3. 缓存原始 segments（`load_segments()`；calibrate=false 或校对未跑时，梗概基于未校对文本，质量略降）
4. 都无 → `SKIPPED_NO_TIMELINE`

**失败可恢复（Codex #5）**：chapters 层缺失/FAILED 时，重提交同 URL 走分层补跑入队（输入从缓存读，成本极低）。`need_chapters` 满足判定：`llm_chapters.json` 存在（GENERATED）或状态 ∈ {SKIPPED_SHORT, SKIPPED_NO_TIMELINE}；FAILED/DISABLED/缺省 → 视为缺失可重跑（对齐 calibration 的 NONE/DISABLED 语义，`transcription.py:551-591`）。

### 5.2 processing_options 语义（Codex #4）

- `chapters` **默认跟随 `summarize` 的生效值**（未显式指定时）：老客户端 `{calibrate:false, summarize:false}` 不会意外触发新的付费调用；显式指定优先。
- 实现点：`normalize_processing_options()`（`transcription.py`）。

### 5.3 ChaptersProcessor

- **压缩输入**：`[i] mm:ss (speaker:)? text`，单次调用。
- **输出 schema（v2.1，Codex #9）**：只回章节起点——`{"chapters": [{"title": str≤20字, "gist": str 2-3句, "start_seg": int}]}`。end 全部由代码推导（`end[i] = start[i+1]-1`，末章 = N-1），从根上消灭重叠/缝隙/非法区间。
- **结构化调用（Codex #8）**：json_object 模式（自带 Self-Correction 格式重试）+ **处理器级语义校验**：start_seg 去重、严格递增、界内；违反时带具体错误重试一次，再失败 → FAILED。
- **门控**：< `min_chapters_threshold`（默认 10000 字符）→ SKIPPED_SHORT；> `max_chapters_input_chars`（默认 500000，**配置注释明确须与 chapters_model 上下文能力匹配**，Codex #13）→ FAILED 并记明确 error。
- **质量结构校验（Codex #14）**：章节数 <2 或 >100 → FAILED；平均每章时长 <60s 或 >40min → 记 warning 保留（诚实记录）；相邻标题完全相同 → 本地合并。
- 时间由代码从 segments 反查，LLM 不经手时间（防抄错）。

### 5.4 状态模型与存储

```python
class ChaptersStatus(StrEnum):
    GENERATED / SKIPPED_SHORT / SKIPPED_NO_TIMELINE / FAILED / PENDING / DISABLED
```

- 仅 GENERATED 写 `llm_chapters.json`（对齐 summary 惯例）；状态入 `llm_status.json`（"非 None 才更新"合并语义，`cache_manager.py:675`）+ `task_status.chapters_status` 列（幂等迁移，仿 `cache_manager.py:266-278`）。
- `llm_chapters.json`：`{format_version, source:{kind, segment_count, fingerprint, generated_at}, chapters:[{index, title, gist, start_seg, start_time, end_time}]}`。fingerprint = 锚点源 dialogs 文本 sha1。
- **recalibrate 联动（Codex #6）**：recalibrate 时若原 chapters_status=GENERATED → 联动重算章节（recalibrate 本就是强制重做语义）；渲染时 fingerprint 与当前 `llm_processed.json` 不一致 → 章节区块照常展示但**去掉跳转链接**（兜底，不静默错跳）。

### 5.5 管线接入

| 层 | 文件 | 改动 |
|---|------|------|
| 配置 | `llm/core/config.py` | `chapters_model` / `chapters_reasoning_effort` / `min_chapters_threshold` / `max_chapters_input_chars` |
| 协调器 | `llm/coordinator.py` | `_generate_chapters_if_needed()`，`stage="chapters"` 审计自动落库 |
| 队列消费 | `api/services/llm_ops.py` | 章节输入梯度解析（§5.1）；`_save_llm_results` chapters 分支（真实产物不被占位覆盖；calibrate_only 路径按 §5.4 联动重算而非跳过） |
| 落盘 | `cache/cache_manager.py` | `save_llm_result(llm_type="chapters")`；`get_cache` 读回；`save_llm_status` 增参 |
| 能力注册 | `cache/cache_analyzer.py` | `cache_files` 加 `"chapters": "llm_chapters.json"` |
| API | `api/services/transcription.py` | `ProcessingOptions.chapters`（默认跟随 summarize）；`need_chapters` 补层判定 |
| 审计 | `api/routes/audit.py` | history/summary 透出 `chapters_status` |

### 5.6 前端

| 文件 | 改动 |
|------|------|
| `dialog_renderer.py` | 结构化渲染的 dialog 块加 `id="dlg-{i}"`（+`data-start-time` 仅展示/未来播放器用）；新增 `render_chapters_html()`：title/gist 一律 `html.escape()` 纯文本 |
| `views.py` | `_prepare_success_view` 读 `llm_chapters.json` + fingerprint 校验 → `chapters_html`；非 GENERATED 不渲染区块 |
| `transcript.html` / `base.html` | 章节区块 + 卡片样式（内联样式约定） |
| `floating-toc.js` | TOC 第三组（章节标题）；跳转直接 `#dlg-{start_seg}` 锚点（Codex #10，弃时间扫描）；**XSS 修复（Codex #11）：章节与现有 TOC 一并改 DOM API + textContent 构建，禁 insertAdjacentHTML 字符串拼接**（现有 TOC `floating-toc.js:169` 同样存在二次注入风险，顺带修） |

## 6. 阶段二：逐段校对推广（方案甲，配置开关独立发布）

**开关**：`llm.structured_calibration_for_plain`（默认 true，可一键回退到整篇重写路径）。关闭时系统行为与现状完全一致（plain 源章节仍可生成，仅无精准跳转）。

**`has_speaker` 模式贯穿全链路（Codex #1，已核验 `speaker_aware_processor.py:228` 缺 speaker 强塞 "unknown"）**：

1. `_coerce_dialogs`：无 speaker 保留缺省，不塞 "unknown"
2. 说话人推断（`:107-127`）：has_speaker=False 时整步跳过（省一次 LLM 调用）
3. `_normalize_and_merge_dialogs`（`:129-132`）：现按"连续同说话人"合并，无说话人会全文并成一坨——改按长度上限合并（300-500 字/块，保首段 start、末段 end）。**本阶段最关键正确性修改**
4. `_build_text_from_dialogs` / 格式化：不输出 "unknown："前缀
5. 校对 prompt 无说话人变体
6. 渲染：无 speaker 不出 speaker-tag，仅 time-tag

**分块与性能（Codex #7 修正）**：结构化路径 dataclass 默认 `preferred=800/max=1500/并发3`（`config.py:73-78`），示例配置 `2000/3000/10`——此前估算误用示例值。无说话人模式增设**独立分块参数**（含 max，建议 `preferred=3000/max=4000`），墙钟影响以生产配置实测为准，实测超预期则调参。

**YouTube 字幕预合并**：进校对前按标点/时间间隔合并成句级。`PlainTextProcessor` 保留为"无 segments 纯文本"降级路径。老数据不迁移。

## 7. 测试计划

**阶段一**：YouTube 三路径解析单测（fixture XML/SRT/snippets：时间保留、DTO 兼容、坏数据容错不丢文本）；CapsWriter 落盘接通；`get_cache` 读回 segments；`load_segments()` 双命名/坏时间置 None/文本不丢。

**阶段三**：chapters_processor（SKIPPED_SHORT / SKIPPED_NO_TIMELINE / 正常 / starts 去重排序校验 / 语义重试 / 密度 warning / 标题合并 / 非法 JSON 耗尽 FAILED）；输入梯度三级 fallback；`need_chapters` 各状态判定；状态合并不抹掉其他字段；迁移幂等；suppress；**recalibrate 联动重算**；fingerprint 不一致去链接；audit 透传；`render_chapters_html` XSS 断言（`<script>`/`<img onerror>` 不逃逸）；TOC DOM API 构建断言；chapters 默认跟随 summarize 的组合矩阵；集成：三来源全链路（mock LLM）→ view 含章节与锚点。

**阶段二**：has_speaker 全链路（不出 "unknown"、跳过推断零调用、合并上限、prompt 变体）；开关关闭 = 现状行为回归；开关开启渲染时间块；旧缓存回归不变。

**质量验收（提前到实现阶段，Codex #14）**：用已有缓存的 3-5 个真实长转录本地跑章节生成，人工评估切分粒度与梗概信息量后再定稿 prompt，不等上线。

## 8. 风险与动手前核实项

1. ~~【必须核实】FunASR 真实字段名~~ **已核实（2026-07-16，n305 生产缓存实测）**：生产 `transcript_funasr.json` 的 segments 字段为 `start_time`/`end_time`（float 秒），另含 `text`/`speaker`/`words`；本仓库 test_cache_dir 旧样例的 `start`/`end` 是过时命名——适配器仍做双命名兼容。
2. ~~【必须核实】`llm_processed.json` 时间格式~~ **已核实（同上）**：dialogs 的 `start_time`/`end_time` 是 `"00:00:41"` 形式的 hh:mm:ss 字符串（截断整秒），另含 `duration`/`original_text`。章节处理器与渲染层必须解析该格式；印证 Codex #10 整秒碰撞判断。
3. 无说话人校对 prompt 变体质量未验证，真实数据试跑。
4. CapsWriter timeline 是 best-effort（上游可能缺 tokens/timestamps），验收措辞已按"尽力+诚实降级"。
5. 阶段二开启后 plain 源校对展示从流式段落变时间块（UX 变化，已接受；开关可回退）。

## 9. NOT in scope

存量 CapsWriter/YouTube 任务回填（无 segments 文件，诚实跳过）；播放器联动；总结"主题详述"瘦身；方案乙（比例估算对齐）；老缓存迁移；tokenizer 级 token 预算（沿项目字符计惯例）。

## 10. What already exists（复用清单）

| 子问题 | 现有实现 | 用法 |
|---|---|---|
| 长度门控 | `min_summary_threshold`（`coordinator.py:403-407`） | 复制 |
| json_object + Self-Correction | `llm.py:472+` | 章节直接复用（注意：json_schema 模式无重试） |
| 诚实状态模型 | `llm_status.py` + 合并写 | ChaptersStatus 并列 |
| 逐段校对+时间保留 | `SpeakerAwareProcessor` + `DialogSegmenter` | 加 has_speaker 模式推广 |
| CapsWriter 时间重建 | `_create_segments_from_capswriter` + funasr 兼容 JSON | 接通落盘断链 |
| 落盘机制 | `save_cache(extra_json_data)` | YouTube/CapsWriter 共用；补 get_cache 读回 |
| token 审计 | contextvars stage | `stage="chapters"` 零开发 |
| 时间块渲染 | `_render_from_structured_data` | 扩展无 speaker |

## 11. 并行实施泳道

```
Lane A（阶段一，可并行）: A1 CapsWriter 落盘+读回  A2 YouTube DTO+三处解析  A3 load_segments 适配器
Lane B（阶段三后端）: 依赖 A —— processor/状态/缓存/API/补层
Lane C（阶段三前端）: 依赖 B 渲染契约 —— 模板/TOC(含 XSS 修复)/样式，可与 B 后半并行
Lane D（阶段二）: 依赖 A，与 B/C 无共享改动面可独立开发，发布顺序在 B/C 之后
冲突提示：A1 与 B 都碰 transcription.py；B 与 C 都碰 dialog_renderer.py —— 同泳道顺序执行
```

## 12. 实施任务清单

- [ ] **T1 (P1)** — 阶段一 A1：CapsWriter `extra_json_data` 接通 + `get_cache` 读回 segments — Codex #3 / 调研断链发现 — `transcription.py`、`cache_manager.py` — 单测+集成
  - 接线批附加项（T2 过渡债）：把 `transcription.py:1016,1308,1312` 的调用切换到 `get_subtitle_result()`/`fetch_for_transcription` 新链路后，删除 `get_subtitle()` 与 `get_subtitle_result()` 的平行分支重复（让旧入口薄委托新入口或直接移除）
- [x] **T2 (P1)** — 阶段一 A2：`SubtitleResult` DTO + YouTube 三处解析保留时间 — ✅ 2026-07-16 完成（feat/chapters-foundation；对外 str 行为逐字节兼容，新入口 `get_subtitle_result()`）
- [x] **T3 (P1)** — 阶段一 A3：`load_segments()`/`normalize_segments()`/`parse_time_to_seconds()`/`sanitize_time_pair()` 适配器 — ✅ 2026-07-16 完成（`transcriber/segments.py`，时间工具权威实现所在地）
- [x] **T4 (P1)** — FunASR 字段名 + `llm_processed.json` 时间格式实测 — ✅ 2026-07-16 完成，结论见 §8.1-2
- [x] **T5 (P1)** — ChaptersProcessor（starts-only schema、json_object+force_json_mode、语义校验、门控、密度校验、原始索引锚定、prompt 压平安全边界）— ✅ 2026-07-16 完成（processor 本体，coordinator 接线属 T6）
- [ ] **T6 (P1)** — 状态/缓存/API/补层接入（含 chapters 默认跟随 summarize、recalibrate 联动重算）— Codex #4/#5/#6 — §5.5 清单 — 单测
- [ ] **T7 (P1)** — 前端：章节区块 + `#dlg-{i}` 锚点 + TOC DOM API 化（XSS）— Codex #10/#11 — §5.6 清单 — XSS 断言+手测
- [ ] **T8 (P2)** — 阶段二：has_speaker 全链路 + 独立分块参数 + 配置开关 — Codex #1/#7 — `speaker_aware_processor.py` 等 — 单测+回归
- [ ] **T9 (P2)** — 真实样本章节质量验收（3-5 例本地跑）→ prompt 定稿 — Codex #14
- [ ] **T10 (P2)** — `docs/features/chapters.md` 功能文档 + processing_options 文档更新

## 实施记录：安全批（2026-07-16 完成）

**范围**：与 pr3-review-hardening 分支零冲突的部分——T2/T3/T4/T5（阶段一的 YouTube+适配器、阶段三的 processor 本体）。T1/T6/T7/T8（接线批：碰 transcription.py / cache_manager.py / llm_ops.py / speaker_aware_processor.py / views.py）等 pr3 合入 main 后基于新 main 开工。

**Codex gate 记录**（完成标准：连续 2 轮无实质新意见）：共 30 轮。R1-R28 累计 47 条发现，44 条采纳修复、3 条裁定驳回（语义重试 by-design、私有方法假想调用方 ×2）；R20 首次干净但 R21 复发计数清零；**R29+R30 连续干净，gate 通过**。修复走向：P1 正确性 bug → 防御性输入处理 → 修复间交互 → 两次构造性根治（时间解析统一到 `parse_time_to_seconds` 单实现；SRT 时间轴判定与 legacy 文本路径共用单一谓词 + 34 输入性质测试）。

**关键工程沉淀**（接线批与后续开发必读）：
1. 时间解析/时间对清洗的**唯一权威实现**在 `transcriber/segments.py`（`parse_time_to_seconds` / `sanitize_time_pair`），任何新消费方一律 import，禁止再写第二套。
2. `chapters_processor` 的 prompt 安全边界：所有外部字符串必须过 `_flatten_for_prompt`（行结构是锚点索引的信任基础）。
3. `Chapter.start_seg/end_seg` 是**原始列表下标**（过滤不重编号），接线时 `#dlg-{i}` 锚点直接可用。
4. llm.py 结构化输出：json_schema 模式无重试；章节用 `force_json_mode="json_object"`。
5. T2 过渡债：接线批切换调用方后删除 `get_subtitle`/`get_subtitle_result` 平行分支（见 T1 附加项）。
6. `capswriter_client.py:221` 存在 `start_time + duration * progress` 求和未做 isfinite 的同类模式（本分支未触碰该文件），T1 实施时顺带修。

## GSTACK REVIEW REPORT

| Review | Trigger | Why | Runs | Status | Findings |
|--------|---------|-----|------|--------|----------|
| CEO Review | `/plan-ceo-review` | Scope & strategy | 0 | — | — |
| Codex Review | `/codex review` | Independent 2nd opinion | 1 | issues_found→absorbed | 14 findings：13 采纳、1 部分采纳；根本性质疑以"章节/校对切换解耦+开关"化解 |
| Eng Review | `/plan-eng-review` | Architecture & tests (required) | 1 | CLEAR | 20 issues（Step0 复杂度门控 1、架构 1、代码质量 4、性能 1、Codex 14 计入），0 critical gaps，全部折入 v2.1 |
| Design Review | `/plan-design-review` | UI/UX gaps | 0 | — | — |
| DX Review | `/plan-devex-review` | Developer experience gaps | 0 | — | — |

- **CODEX:** 独立复核 14 条：json_schema 无重试、无说话人模式缺失、segments 读回断链等 3 条经逐行核验属实并纠正本审查错误前提；starts-only schema 与直接锚点两项方案性改进被采纳。
- **CROSS-MODEL:** 张力点唯一——"校对管线重构与功能发布耦合"。解法：章节仅依赖 timeline 层，阶段二加开关独立灰度，实施顺序 一→三→二（用户已批准）。
- **VERDICT:** ENG CLEARED — ready to implement（阶段一可立即开工）。

NO UNRESOLVED DECISIONS
