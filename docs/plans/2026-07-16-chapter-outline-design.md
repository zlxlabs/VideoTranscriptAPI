# 长逐字稿章节梗概（chapters）功能设计 v2.1.2

- **日期**：2026-07-16（v2.1）；**接线规格修订 2026-07-19（v2.1.1）**；**阶段二方案修订 2026-07-19（v2.1.2）**
- **状态**：**安全批已完成并通过 Codex gate**（30 轮，R29+R30 连续两轮无实质新意见）；**pr3（#12/#13/#14）已合入 origin/main**；**2026-07-19 已 rebase 到 origin/main**（无冲突，unit+llm 2292 passed）；接线批（T1/T6–T10）可开工。全量 Eng Review **不重跑**（架构仍 CLEAR）；接线按 §5.7。
- **分支**：`feat/chapters-foundation`（已含 pr3 基线 + 安全批 + v2.1.1 文档；未 push）
- **依据**：三份代码调研 + 关键路径逐行核验 + Codex 独立复核；2026-07-19 对照 origin/main 只读核对 §5.x
- **任务卡**：`docs/sessions/260719-0513-chapters/TASKS.md`；交接：`docs/sessions/260719-0513-chapters/HANDOFF.md`

## 版本变更记录

- **v1 → v2**：存量任务忽略（D4）；路线从"按来源分期"改为"先地基后功能"；校对拍板方案甲（D5）。
- **v2 → v2.1**（Codex 复核后）：**章节与校对切换解耦**——章节只依赖 timeline 数据层，阶段二变为带开关的独立升级；实施顺序调整为 **阶段一 → 阶段三 → 阶段二**；恢复 chapters-only 补层（失败可恢复）；章节输出 schema 改为 starts-only；跳转改直接 seg 锚点；json_object 模式替代 json_schema；`has_speaker` 模式贯穿全链路；XSS 修复扩展到现有 TOC；其余见 §13 处置表。
- **v2.1 → v2.1.1**（2026-07-19，pr3 合入后只读核对）：不改产品决策 D1–D6；修订接线落点与 pr3 加固约束——`ProcessingOptions` 路径、`chapters` 默认跟随 summarize 的建模、分层 `need_chapters` 状态敏感判定、`_save_llm_results` 的 media_lock / write-ahead `invalidate_llm_status`、recalibrate 与 summary backfill 的差异。详见 §5.7。**不重跑全量 Eng Review**。
- **v2.1.1 → v2.1.2**（2026-07-19，用户拍板阶段二方案，不改 D1–D6）：**校对与段落化解耦**——校对结构保持（id 映射、禁止合并/拆分/重排），段落化只选边界、不动文本，**先校准后段落化**；§6 第 3 点原"按长度上限合并"作废（语义盲），改为校准后确定性段落化（长度预算 + 句末/停顿授权，规格见 TASKS T8）；开关 `structured_calibration_for_plain` 默认由 true 改为 **false 暗启动**（T9 验证后再翻）；v2 LLM 语义段落化（提议+吸附，starts-only 契约）列为可选后续升级。沿用 v2.1.1 先例：**不重跑全量 Eng Review**，仅对 T8 变化面做增量核对。同日增量 review（判 NEEDS_REVISION 后收敛）：手术点补全为 speaker 6 处 + 时间 2 处；钉死段落化集成点（processor 内、三者同一列表）；`_prepare_llm_content` 仅 calibrate 请求时返回 list；算法终端规则补全（ASCII 标点、2×hard_max 硬切、授权点取向）；key_info 提取保留（仅跳 SpeakerInferencer.infer）；开关回退语义定为方案 b（provenance 标记 + 开关关时渲染忽略）；渲染器一处防御性修改（`d["speaker"]`→`.get`）。

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

**失败可恢复（Codex #5）**：chapters 层缺失/FAILED 时，重提交同 URL 走分层补跑入队（输入从缓存读，成本极低）。

**`need_chapters` 满足判定（状态敏感，对齐 calibration 而非 summary 的「纯文件存在」）**：

| 条件 | 层是否满足（不必重跑） |
|------|------------------------|
| `llm_status.chapters_status == GENERATED` 且 `llm_chapters.json` 存在 | 是 |
| `chapters_status ∈ {SKIPPED_SHORT, SKIPPED_NO_TIMELINE}` | 是（正常跳过，诚实终态） |
| `chapters_status ∈ {FAILED, DISABLED}` 或状态缺省/文件与状态不一致 | 否（可重跑；DISABLED 仅当本轮 `chapters=true` 时视为需生成） |
| 仅有文件无状态 / 仅有状态无文件 | 否（保守重跑，避免混血） |

实现落点：`api/services/transcription.py` 的分层缓存命中分支（与 `need_calibrated` / `need_summary` / `need_speaker_names` 并列），**以符号名为准，禁止依赖过时行号**。

### 5.2 processing_options 语义（Codex #4）

- `chapters` **默认跟随 `summarize` 的生效值**（请求体未显式出现 `chapters` 时）：老客户端 `{calibrate:false, summarize:false}` 不会意外触发新的付费调用；显式指定优先。
- **实现落点（v2.1.1 修订）**：`src/video_transcript_api/api/processing_options.py` 的 `ProcessingOptions` + `normalize_processing_options()`（pr3 已从 `transcription.py` 抽出；`transcription` / `llm_ops` / recalibrate 路由均 import 此处）。
- **建模约束（禁止踩坑）**：
  - **禁止** `chapters: StrictBool = True` 与现有三字段同款默认——缺省全 true 会让「只关 calibrate/summarize」的老请求仍跑章节。
  - 推荐：`chapters: Optional[StrictBool] = None`（或等价「字段未出现」语义），在 `normalize_processing_options()` 内：`None → 采用规范化后的 summarize 值`；显式 `true/false` 原样保留。
  - `extra="forbid"` 保持：未知字段仍 422。
- **`_requires_llm_title` 等副作用**：仅 `chapters=true` 时仍应能入队 LLM 阶段；是否触发 title 生成保持与「无 calibrate/summarize/infer 时不额外为 title 付费」一致——**仅 chapters 不单独触发 title**（小裁决，实现时写单测锁死）。

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

- 仅 GENERATED 写 `llm_chapters.json`（对齐 summary：确定性产物才落盘；SKIPPED_* / FAILED / DISABLED 不写章节文件，只写状态）。
- 状态入 `llm_status.json`（`save_llm_status` 的「非 None 才更新」合并语义）+ `task_status.chapters_status` 列（幂等 `ALTER TABLE ... ADD COLUMN`，仿现有 `calibration_status` / `summary_status` 迁移；**以 `CacheManager` 迁移循环为准，勿抄旧行号**）。
- `llm_chapters.json`：`{format_version, source:{kind, segment_count, fingerprint, generated_at}, chapters:[{index, title, gist, start_seg, start_time, end_time}]}`。fingerprint = 锚点源 dialogs 文本 sha1。
- **recalibrate 联动（Codex #6，注意与 summary 差异）**：
  - 现网 summary：`calibrate_only` 时默认**不动** summary，仅当 `llm_summary` 缺失时 `_should_backfill_summary`。
  - chapters：**若原 `chapters_status=GENERATED` → 强制联动重算**（recalibrate 本就是强制重做语义；校对文本/锚点源已变）。SKIPPED_* 可保留或按本轮 `chapters` 开关再判定；FAILED 可重试。
  - 渲染时 fingerprint 与当前 `llm_processed.json` 不一致 → 章节区块照常展示但**去掉跳转链接**（T7 兜底，不静默错跳）。

### 5.5 管线接入（v2.1.1 路径）

| 层 | 文件 | 改动 |
|---|------|------|
| 配置 | `llm/core/config.py` | 安全批已加：`chapters_model` / `chapters_reasoning_effort` / `min_chapters_threshold` / `max_chapters_input_chars`（rebase 后保留） |
| 状态常量 | `utils/llm_status.py` | 安全批已加 `ChaptersStatus`；接线批消费之 |
| 协调器 | `llm/coordinator.py` | `_generate_chapters_if_needed()`；`with set_context(stage="chapters")`（与 calibration/summary 同模式） |
| 队列消费 | `api/services/llm_ops.py` | 章节输入梯度（§5.1）；`_save_llm_results` chapters 分支；**必须**进入现有 `media_lock` + write-ahead `invalidate_llm_status` 流程；`suppress`/只增不减；calibrate_only 按 §5.4 联动重算 |
| 落盘 | `cache/cache_manager.py` | `save_llm_result(llm_type="chapters")` → `llm_chapters.json`；`get_cache` 读回；`save_llm_status(..., chapters_status=)`；`task_status` 列迁移；`update_task_status` 透传 |
| 能力注册 | `cache/cache_analyzer.py` | `cache_files` 加 `"chapters": "llm_chapters.json"` |
| Options | **`api/processing_options.py`** | `chapters` 字段 + normalize 跟随 summarize（§5.2）；**不要**改回 transcription.py 内联定义 |
| 分层入队 | `api/services/transcription.py` | `need_chapters` 补层判定（§5.1 表）；与 need_calibrated/summary/speaker_names 并列入队 |
| 审计 | `api/routes/audit.py` | history 透出 `chapters_status`；summary 端点按需透出（对齐 calibration/summary 字段模式） |

### 5.6 前端

| 文件 | 改动 |
|------|------|
| `utils/rendering/dialog_renderer.py` | 结构化渲染 dialog 块加 `id="dlg-{i}"`（+`data-start-time` 仅展示/未来播放器用）；新增 `render_chapters_html()`：title/gist 一律 `html.escape()` |
| `api/routes/views.py` | `_prepare_success_view` 读 `llm_chapters.json` + fingerprint 校验 → `chapters_html`；非 GENERATED 不渲染区块 |
| `web/templates/transcript.html` / `base.html` | 章节区块 + 卡片样式 |
| `web/static/js/floating-toc.js` | TOC 第三组（章节标题）；跳转 `#dlg-{start_seg}`；**XSS：章节与现有 TOC 一并改 DOM API + textContent，禁 `insertAdjacentHTML` 字符串拼接**（origin/main 仍在用 `insertAdjacentHTML`，债未消） |

### 5.7 接线规格修订（2026-07-19 / pr3 合入后只读核对）

**结论**：产品架构与 Eng CLEAR 的 v2.1 决策未被 pr3 推翻；**不重跑**全量 `/plan-eng-review`。下列为接线时必须遵守的「现网真相」，替代计划旧行号与过时路径。

| # | 主题 | 修订内容 |
|---|------|----------|
| R1 | Options 路径 | `ProcessingOptions` / `normalize_processing_options` 在 `api/processing_options.py` |
| R2 | chapters 默认值 | 未显式指定 → 跟随 summarize；禁止默认恒 `True`（§5.2） |
| R3 | need_chapters | 状态敏感（§5.1 表），**不要**只抄 `need_summary` 的「有文件即满足」 |
| R4 | 落盘并发与一致性 | `_save_llm_results` 整段 `media_lock`；写产物前 `invalidate_llm_status` write-ahead；失败不得留下新旧混血；chapters 写入同一事务语义 |
| R5 | suppress / 只增不减 | 已有真实 GENERATED 且本轮未请求 chapters → 不覆盖；DISABLED 占位与 GENERATED 区分（对齐 calibration/summary 诚实状态） |
| R6 | recalibrate | 与 summary backfill **不同**：GENERATED chapters **强制重算**；勿误抄「calibrate_only 不动 summary」 |
| R7 | title 副作用 | 仅 chapters 不单独触发 `_requires_llm_title` |
| R8 | 行号 | 计划内所有旧行号作废；以函数名 / 行为契约为准 |
| R9 | T1 断链仍在 | `save_cache` 支持 `extra_json_data`，但 transcription 全路径未传；`get_cache` 不读 timeline segments；YouTube 仍 `get_subtitle` 纯文本 |
| R10 | 安全批已备零件 | `ChaptersProcessor`、`segments.py`、`SubtitleResult`、`ChaptersStatus`、config 字段均在 foundation；接线批消费，不重造 |

**Eng Review 策略**：默认不重跑。仅当 rebase 后发现状态模型/分层缓存/options 语义与 §5 表冲突、或要改 D1–D6 时，对 **T6 变化面**做增量 Eng 核对。

## 6. 阶段二：逐段校对推广（方案甲，配置开关独立发布）

**开关**：`llm.structured_calibration_for_plain`（**默认 false，暗启动**——v2.1.2 修订，原为默认 true；T9 真实样本验证通过后再翻 true）。关闭时系统行为与现状完全一致（plain 源章节仍可生成，仅无精准跳转）。

**校对与段落化解耦（v2.1.2 核心修订）**：校对结构保持（id 映射、禁止合并/拆分/重排），段落化只选边界、不动文本；**先校准、后段落化**——校准先修好标点，段落化再消费标点信号选边界。落盘的 `llm_processed.json` dialogs 即段落（与 FunASR 同构），渲染/章节/指纹契约零改动复用。确定性段落化算法完整规格（句末授权 / 停顿授权 / 硬上限兜底 / 时间缺失降级）见 TASKS.md T8 卡。v2 LLM 语义段落化（LLM 提议断点 + 本地吸附，starts-only 契约，失败回退 v1）为可选后续升级，由 T9 读感评估决定是否启动。

**`has_speaker` 模式贯穿全链路（Codex #1，已核验 `speaker_aware_processor.py:228` 缺 speaker 强塞 "unknown"）**：

1. `_coerce_dialogs`：无 speaker 保留缺省，不塞 "unknown"
2. 说话人推断（`:107-127`）：has_speaker=False 时整步跳过（省一次 LLM 调用）
3. `_normalize_and_merge_dialogs`（`:129-132`）：现按"连续同说话人"合并，无说话人会全文并成一坨——has_speaker=False 时**不按 speaker 合并**，保持原始 segments 粒度进校准，校准后再做确定性段落化（v2.1.2 修订；原"按长度上限合并"作废）。**本阶段最关键正确性修改**
4. `_build_text_from_dialogs` / 格式化：不输出 "unknown："前缀
5. 校对 prompt 无说话人变体
6. 渲染：无 speaker 不出 speaker-tag，仅 time-tag

> **增量 review（2026-07-19）补录**：手术点最终清单以 TASKS T8 卡为准——speaker 维度 6 处（上述 1/2/3/4 之外，新增 `_normalize_dialog`、`_apply_corrections_by_id` 两处 unknown 注回点；第 2 点明确为仅跳 SpeakerInferencer.infer，**key_info 提取保留**，它喂养校对 prompt、与 speaker 无关）+ 时间维度 2 处（None 时间不兜底 `"00:00:00"`）；段落化集成点钉死在 processor 内 `structured_data` 返回前（`llm_processed.json`、章节输入、渲染锚点三者同一列表），且消费原始 float 秒而非 HH:MM:SS 截断字符串；`_prepare_llm_content` 仅 calibrate 请求时返回 list（防补层永久 nolink）；算法终端规则补全（ASCII 标点、2×hard_max 硬切、授权点取向）；渲染器一处防御性修改（`d["speaker"]`→`.get`，缺键 KeyError 会崩主视图）；开关回退语义定为方案 b（plain 结构化产物写 provenance `"mode": "plain_structured"`，开关关时渲染忽略、不删文件）。

**分块与性能（Codex #7 修正）**：结构化路径 dataclass 默认 `preferred=800/max=1500/并发3`（`config.py:73-78`），示例配置 `2000/3000/10`——此前估算误用示例值。无说话人模式增设**独立分块参数**（含 max，建议 `preferred=3000/max=4000`），墙钟影响以生产配置实测为准，实测超预期则调参。

**YouTube 字幕预合并**：进校对前按标点/时间间隔合并成句级（与段落化共用同一确定性边界工具、不同参数；段落断点必为预合并 unit 边界，两步不打架）。`PlainTextProcessor` 保留为"无 segments 纯文本"降级路径。老数据不迁移。

## 7. 测试计划

**阶段一**：YouTube 三路径解析单测（fixture XML/SRT/snippets：时间保留、DTO 兼容、坏数据容错不丢文本）；CapsWriter 落盘接通；`get_cache` 读回 segments；`load_segments()` 双命名/坏时间置 None/文本不丢。

**阶段三**：chapters_processor（SKIPPED_SHORT / SKIPPED_NO_TIMELINE / 正常 / starts 去重排序校验 / 语义重试 / 密度 warning / 标题合并 / 非法 JSON 耗尽 FAILED）；输入梯度三级 fallback；`need_chapters` 各状态判定；状态合并不抹掉其他字段；迁移幂等；suppress；**recalibrate 联动重算**；fingerprint 不一致去链接；audit 透传；`render_chapters_html` XSS 断言（`<script>`/`<img onerror>` 不逃逸）；TOC DOM API 构建断言；chapters 默认跟随 summarize 的组合矩阵；集成：三来源全链路（mock LLM）→ view 含章节与锚点。

**阶段二**（v2.1.2 修订）：has_speaker 全链路（`llm_processed.json` 序列化全文不含 "unknown"、SpeakerInferencer.infer 零调用、key_info 保留、prompt 变体与 echo 拒绝正则对齐）；确定性段落化算法（句末/停顿/硬上限/终端硬切授权全集、时间 None、整篇无标点收敛、单段超 hard_max、target 前不提前断）；开关关闭 = 现状行为回归；开关开启 plain 源渲染 `dlg-{i}` 锚点 + `jump_ok=1` 集成；时间 None 段落无 "00:00:00" 标签；chapters-only 补层不触发本轮段落化（`_prepare_llm_content` 豁免）；开关回退（方案 b）渲染忽略 plain 结构化产物；recalibrate 姓名恢复 no-op；旧缓存回归不变。

**质量验收（提前到实现阶段，Codex #14）**：用已有缓存的 3-5 个真实长转录本地跑章节生成，人工评估切分粒度与梗概信息量后再定稿 prompt，不等上线。

## 8. 风险与动手前核实项

1. ~~【必须核实】FunASR 真实字段名~~ **已核实（2026-07-16，n305 生产缓存实测）**：生产 `transcript_funasr.json` 的 segments 字段为 `start_time`/`end_time`（float 秒），另含 `text`/`speaker`/`words`；本仓库 test_cache_dir 旧样例的 `start`/`end` 是过时命名——适配器仍做双命名兼容。
2. ~~【必须核实】`llm_processed.json` 时间格式~~ **已核实（同上）**：dialogs 的 `start_time`/`end_time` 是 `"00:00:41"` 形式的 hh:mm:ss 字符串（截断整秒），另含 `duration`/`original_text`。章节处理器与渲染层必须解析该格式；印证 Codex #10 整秒碰撞判断。
3. 无说话人校对 prompt 变体质量未验证，真实数据试跑。
4. CapsWriter timeline 是 best-effort（上游可能缺 tokens/timestamps），验收措辞已按"尽力+诚实降级"。
5. 阶段二开启后 plain 源校对展示从流式段落变时间块（UX 变化，已接受）。开关回退语义（v2.1.2 方案 b）：关 = 新任务走旧路径；已产出的 plain 结构化产物凭 provenance `"mode": "plain_structured"` 标记在开关关时被渲染忽略，**不删除**（只增不减）。

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

> 可派发任务卡正文见 `docs/sessions/260719-0513-chapters/TASKS.md`（v2.1.1）。依赖：T1 → T6 → T7∥T8；T9 可在 T5 后本地跑；T10 收尾。

- [x] **T1 (P1)** — 阶段一 A1：CapsWriter `extra_json_data` 接通 + `get_cache` 读回 segments（经 `load_segments`）— `api/services/transcription.py`、`cache/cache_manager.py` — 单测+集成 — ✅ 2026-07-19
  - 附加：YouTube 调用切 `get_subtitle_result()`；旧 `get_subtitle` 薄委托或删平行分支；`capswriter_client` 时间求和 `isfinite` 校验
- [x] **T2 (P1)** — 阶段一 A2：`SubtitleResult` DTO + YouTube 三处解析保留时间 — ✅ 2026-07-16
- [x] **T3 (P1)** — 阶段一 A3：`transcriber/segments.py` 时间工具权威实现 — ✅ 2026-07-16
- [x] **T4 (P1)** — FunASR 字段名 + `llm_processed.json` 时间格式实测 — ✅ 2026-07-16
- [x] **T5 (P1)** — ChaptersProcessor 本体 — ✅ 2026-07-16（coordinator 接线属 T6）
- [x] **T6 (P1)** — 状态/缓存/API/补层接入 — **按 §5.5 + §5.7**（Options 在 `processing_options.py`；media_lock/write-ahead；need_chapters 状态敏感；recalibrate 强制重算 GENERATED chapters）— ✅ 2026-07-19
- [x] **T7 (P1)** — 前端章节区块 + `#dlg-{i}` + TOC DOM API（XSS）— §5.6 — ✅ 2026-07-19
- [x] **T8 (P2)** — 阶段二：逐段校对推广 v1（确定性段落化，规格以 TASKS.md T8 节为准，v2.1.2 修订）— ✅ 2026-07-19
- [x] **T9 (P2)** — 真实样本 3–5 例质量验收 → prompt 定稿 — ✅ 2026-07-19（3 样本全 jump_ok，`structured_calibration_for_plain` 默认翻 true，见 REAL-SAMPLE-TEST.md）
- [x] **T10 (P2)** — `docs/features/chapters.md` + `processing_options.md` 补充 chapters — ✅ 2026-07-19
- [x] **T11（计划外追加）** — 章节 UI 重设计（速览面板 + 移动端吸顶条/抽屉，`#chapters-data` 数据岛）— ✅ 2026-07-19，见 `docs/sessions/260719-1155-t8-implementation/CHAPTER-UI-REDESIGN.md`

> **2026-07-23 对账**：接线批 T1/T6-T10 与计划外 T11 已全部完成，经 stacked PR #15-#25 合入 main 并部署生产（`ddb4ce6`）。本清单此前未同步勾选。过程记录见 `docs/sessions/260719-0547-chapters-wiring/`、`docs/sessions/260719-1155-t8-implementation/`（含 REVIEW-LOG、REAL-SAMPLE-TEST、STACKED-PR-AND-DEPLOY）。

## 实施记录：安全批（2026-07-16 完成）

**范围**：与当时 pr3-review-hardening 零冲突的部分——T2/T3/T4/T5。T1/T6/T7/T8 等接线批在 pr3 合入 main 后基于新 main 开工（**2026-07-19：pr3 已通过 #12/#13/#14 合入 origin/main**）。

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

### 2026-07-19 接线前只读核对（非全量 Eng Review）

- **范围**：对照 origin/main（含 pr3）与本计划 §5.x / §12。
- **结论**：架构与 D1–D6 仍成立；全量 Eng Review **不重跑**。
- **产出**：v2.1.1 修订（§5.1 need 表、§5.2 Options 路径与默认值建模、§5.4 recalibrate 差异、§5.5 路径表、**§5.7 十条接线约束**）+ `TASKS.md` 任务卡。
- **剩余前置**：~~rebase + 基线测试~~ ✅ 2026-07-19（无冲突，2292 passed）。下一步按 T1→T6 开工。
