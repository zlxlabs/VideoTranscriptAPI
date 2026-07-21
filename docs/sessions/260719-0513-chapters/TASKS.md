# 章节梗概接线批 —— 任务卡（v2.1.2）

> 创建：2026-07-19  
> 计划：`docs/plans/2026-07-16-chapter-outline-design.md`（v2.1.2）  
> 分支：`feat/chapters-foundation`（rebase 到含 pr3 的 origin/main 后开工）  
> 纪律：TDD；主脑派 subagent（sonnet）实现；连续 2 轮 Codex gate 干净才收工；不 push / 不合并 main，除非用户授权。

## 依赖与泳道

```
T1 (timeline 断链) ──► T6 (管线/状态/API) ──┬──► T7 (前端)
                                            ├──► T8 (阶段二校对，P2，可并行)
                                            └──► T10 (文档，可偏后)
T5 已完成 ──► T9 (真实样本质量，可与 T6 后并行)
```

| 任务 | 优先级 | 依赖 | 可并行 |
|------|--------|------|--------|
| T1 | P1 | rebase 完成 | 单独先做 |
| T6 | P1 | T1 | — |
| T7 | P1 | T6（至少契约：产物路径 + dlg 索引） | 与 T8 并行 |
| T8 | P2 | T1（timeline）；建议 T6 后 | 与 T7 并行 |
| T9 | P2 | T5（可本地直接调 processor）；建议有 T1 真实 segments | 独立 |
| T10 | P2 | T6 语义稳定 | 收尾 |

---

## T1 — CapsWriter/缓存 timeline 接通 + YouTube 过渡债

**目标**：三种来源新任务都能尽力落统一契约的 segments；`get_cache` / `load_segments` 可读回。

**背景**：`save_cache(..., extra_json_data=)` 已有写能力，但 `transcription.py` 全路径未传；`get_cache` 不组装 timeline；YouTube 仍 `get_subtitle()` 纯文本。

**改动面（以符号为准）**：

- `api/services/transcription.py`：CapsWriter 落盘传 `extra_json_data`；YouTube 字幕路径改 `get_subtitle_result()` 并落 segments
- `cache/cache_manager.py`：`get_cache` 读回 segments 侧车（或保证 `load_segments(cache_dir)` 能找到约定文件名）
- `transcriber/capswriter_client.py`：时间求和 `isfinite` 校验（顺带）
- YouTube：旧 `get_subtitle` 薄委托 `get_subtitle_result` 或删除平行分支重复

**约束**：

- 时间解析只 import `transcriber.segments`，禁止第二套
- 上游缺 tokens/timestamps 时不阻断任务，诚实降级
- CRLF 文件用 Edit，勿 Python 整文件覆盖

**测试**：

- 单测：extra_json 落盘 + get_cache/load_segments 读回；坏时间置 None 文本不丢
- YouTube：调用新入口后 segments 非空（fixture）
- capswriter isfinite 边界

**完成标准**：相关单测绿；无 segments 时任务仍 success 且不崩溃。

---

## T6 — 状态 / 缓存 / API / 补层全链路

**目标**：请求可开关 chapters；协调器生成；落盘状态诚实；分层补层可恢复；recalibrate 联动。

**必读**：计划 §5.1–5.5、**§5.7（R1–R10）**。

**改动面**：

| 层 | 文件 |
|----|------|
| Options | `api/processing_options.py`（**不是** transcription 内联） |
| 分层入队 | `api/services/transcription.py`（`need_chapters`） |
| 协调器 | `llm/coordinator.py`（`stage="chapters"`） |
| 队列/落盘编排 | `api/services/llm_ops.py`（`_save_llm_results`） |
| 缓存 | `cache/cache_manager.py`（`llm_type=chapters`、status 合并、DB 列） |
| 分析器 | `cache/cache_analyzer.py` |
| 审计 | `api/routes/audit.py` |
| 常量 | `utils/llm_status.py`（`ChaptersStatus` 已在 foundation） |

**硬约束（pr3 后）**：

1. `chapters=None`（未指定）→ normalize 后等于 `summarize`；禁止默认恒 True  
2. `need_chapters` 状态敏感（GENERATED+文件 / SKIPPED_* 满足；FAILED/DISABLED/缺省可重跑）  
3. 写入走 `media_lock` + write-ahead `invalidate_llm_status`  
4. recalibrate：原 GENERATED chapters **强制重算**（≠ summary 仅缺失 backfill）  
5. 仅 chapters=true **不**单独触发 title LLM  
6. 外部串进 prompt 必经 `_flatten_for_prompt`

**测试（优先单测，再小集成）**：

- normalize 矩阵：未传 / 显式 true / 显式 false × summarize 组合  
- need_chapters 各状态  
- save 合并不抹掉 calibration/summary  
- suppress 不覆盖已有 GENERATED  
- recalibrate 联动重算  
- 迁移幂等  

**完成标准**：mock LLM 下 FunASR 路径可出 `llm_chapters.json` + 正确 status；分层二次请求不重复付费（满足态）。

---

## T7 — 前端章节区块 + 锚点 + TOC XSS

**依赖**：T6 产物契约（`llm_chapters.json`、`start_seg` 原始下标）。

**改动面**：

- `utils/rendering/dialog_renderer.py`：`id="dlg-{i}"`、`render_chapters_html()`
- `api/routes/views.py`：`_prepare_success_view`
- `web/templates/transcript.html` / `base.html`
- `web/static/js/floating-toc.js`：DOM API，禁 `insertAdjacentHTML` 拼用户/章节标题

**测试**：XSS 断言（`<script>`/`onerror` 不执行）；fingerprint 不一致去链接；手测 TOC 三组。

---

## T8 — 阶段二：逐段校对推广 v1（P2，独立开关，确定性段落化）

> **2026-07-19 方案修订（用户拍板）**：校对与段落化解耦——校对结构保持（id 映射、禁止合并/拆分/重排），段落化只选边界、不动文本；**先校准、后段落化**。原「按长度上限合并」作废（语义盲：可能跨说话人并段、腰斩完整语义）。v2 LLM 语义段落化为可选后续升级，是否启动由 T9 读感评估决定。
>
> **2026-07-19 增量 review 收敛**（对照代码核验，判 NEEDS_REVISION 后已修）：手术点由 4 处补全为 **speaker 6 处 + 时间 2 处**；钉死段落化集成点；`_prepare_llm_content` 补 calibrate 豁免；算法终端规则补全；key_info 提取保留（仅跳 SpeakerInferencer.infer）；新增开关回退语义（方案 b）。

**开关**：`llm.structured_calibration_for_plain`，默认 **false**（暗启动；T9 真实样本验证通过后再翻 true；修订前文档为默认 true，以此为准）。关闭时 plain 路径行为与现状完全一致（章节仍可生成，仅无精准跳转）。

**改动面**：

| 层 | 文件 | 改动 |
|----|------|------|
| 配置 | `llm/core/config.py` | `structured_calibration_for_plain`（dataclass 末尾追加，保持位置参数兼容）；无 speaker 独立分块参数入 `llm.structured_calibration.*` 段（建议 preferred=3000 / max=4000，命名与现有段对齐）；段落化三参数入新段 `llm.paragraphization.*`（`target_chars=300` / `hard_max_chars=600` / `pause_threshold_seconds=2.0`） |
| 处理器 | `llm/processors/speaker_aware_processor.py` | has_speaker 模式贯穿（手术点见下）；`__init__` 支持按 has_speaker 注入 DialogSegmenter 独立分块参数 |
| 段落化 | **新工具，落 `transcriber/segments.py`（或同包新模块）** | 确定性段落化（规格见下）。落点理由：依赖方向已是 llm→transcriber（chapters_processor 同例），段落化是零 LLM 依赖的 timeline 结构工具，与「时间工具唯一权威」同源；`llm/segmenters/` 两个既有 segmenter 是「为 prompt 分块」语义，不混入 |
| 编排 | `api/services/llm_ops.py` | `_prepare_llm_content`：**仅 `calibrate_requested=True`**（含 recalibrate）且 plain 源有 segments → 返回 list（协调器 isinstance 路由自然生效）；calibrate=false 补层任务维持纯文本（防止本轮未校准段落覆盖章节输入、与缓存已校准段落指纹永久 mismatch → 永久 nolink）；structured 落盘 gate（现仅 `use_speaker_recognition`）扩展到本模式；plain 结构化产物写 provenance `"mode": "plain_structured"`；`llm_calibrated.txt` 继续生成（无前缀变体，保 export/raw 兼容） |
| prompt | `llm/prompts/__init__.py` + 处理器 `_format_chunk_for_prompt` | 无说话人变体：行格式去 `[speaker]`（system prompt 描述与实际行生成两处同步）；`_valid_correction_text` 的 echo 拒绝正则与新行格式对齐；契约不变（`{id,text}` 输出、禁止合并/拆分/增删/重排） |
| 渲染 | `utils/rendering/dialog_renderer.py` | **一处防御性修改**：`_render_from_structured_data` 的 speakers 收集改 `d.get("speaker")`（现为 `d["speaker"]` 下标，缺键即 KeyError 崩主视图）；其余无改动（已支持无 speaker 时间块 + `dlg-{i}` 锚点） |
| 章节/指纹 | 无改动 | 输入梯度自动取到本轮段落化后的 dialogs |

**has_speaker 判定**：`has_speaker = any(SpeakerInferencer.resolve_dialog_speaker(d) is not None for d in dialogs)`；混合输入（部分段有 speaker）维持现状（has_speaker=True，缺省段塞 "unknown"）。

**手术点（speaker 维度 6 处，`speaker_aware_processor.py`）**：

1. `_coerce_dialogs`：缺 speaker 保留缺省，**不塞 "unknown"**；
2. **SpeakerInferencer.infer 整步跳过**——零 LLM 调用。**key_info 提取在校准开启时保留**（它喂养校对 prompt、与 speaker 无关；现状 plain 路径也提取，整步跳过会造成校对质量回退）；
3. `_normalize_dialog`：has_speaker=False 时 speaker/speaker_id 保留缺省（现 `dialog.get("speaker", "unknown")` 会注回）；
4. `_normalize_and_merge_dialogs`：无 speaker 时**不按 speaker 合并**（保持原始 segments 粒度进校准；避免全文塌缩成一条再被硬拆、时间戳被复制污染）；
5. `_apply_corrections_by_id`：speaker 透传缺省（现 `original.get("speaker", "unknown")` 落盘前再次兜底）；
6. `_build_text_from_dialogs`：无 speaker 变体，不输出 `speaker：`前缀。

**手术点（时间维度 2 处）**：

7. `_normalize_dialog`：has_speaker=False 时 None 时间保留 None（现兜底 `"00:00:00"` 会让每个缺时间段落挂 "00:00:00" 标签；下游契约已验证兼容 None：chapters `_to_seconds`、指纹、渲染时间标签均容忍）；
8. `_apply_corrections_by_id`：start/end 透传 None（现 `original.get(..., "00:00:00")`）。

**段落化集成点（钉死）**：段落化在 `SpeakerAwareProcessor.process()` 内、构造 `structured_data` 返回**之前**执行——`llm_processed.json` dialogs、章节输入（coordinator 取 `calibration_result.structured_data.dialogs`）、渲染锚点三者是**同一个列表**。`skip_calibration=True`（calibrate=false 首跑）时仍段落化（确定性、零 LLM），段落=未校准文本，渲染形态与 FunASR calibrate=false 一致（时间块 + 原文）。**段落化消费原始 segments 的 float 秒**（normalize 前快照时间轴），不消费落盘的 HH:MM:SS 截断字符串（`int(seconds)` 截断会让停顿授权在 ±2s 误差下误判）。

**确定性段落化算法规格（v1）**：

- 输入：校准后的 segments（`text`/`start_time`/`end_time`，时间可为 None）。
- 输出：段落 `[{start_time=首段 start, end_time=末段 end, text=校准文本拼接, original_text=原文拼接}]`（无 speaker 键），落盘为 `llm_processed.json` 的 dialogs（与 FunASR 同构；渲染/章节/指纹复用现有契约）。
- 参数（`llm.paragraphization.*`）：`target_chars≈300`（长度预算）、`hard_max_chars≈600`（硬上限）、`pause_threshold_seconds≈2.0`（停顿授权阈值）。
- 标点集合：句末 `。！？…` + ASCII `.!?`（兼容标点后引号/括号收尾）；逗号级 `，；：` + `,;:`。
- 规则（**长度只是预算不是闸刀**——到预算开始找断点，授权点才真的断）：
  1. **句末授权**：仅在句末标点后允许断段；
  2. **停顿授权**：`next.start_time - cur.end_time ≥ pause_threshold` 时允许断（无需句末标点；对话停顿大概率是说话人转换）；
  3. **硬上限兜底**：到 hard_max 仍无授权点 → 放宽到逗号级；断点取 **hard_max 之前最后一个授权点**（取不到则取之后第一个）；
  4. **终端规则**（整篇无标点，如英文 YouTube 自动字幕）：窗口放宽到 2×hard_max 仍无任何授权点 → 在 2×hard_max 前最后一个空白/词边界**硬切**并记 warning（保证收敛，不产出整篇一段）；
  5. **病理兜底**：单段即超 hard_max → 原样保留不切，记 warning；
  6. **时间缺失**：相邻段时间为 None → 停顿信号不可用，仅用标点授权；
  7. **target 之前的授权点不提前断**（保持长度预算语义）。
- YouTube 句级预合并（进校对前）复用同一工具、不同参数（句级 target 更小）；两步不打架：段落断点必为预合并 unit 边界，授权规则在 unit 边界上重判，unit 内句末标点不作断点。

**开关回退语义（方案 b）**：开关关 = 新任务走旧 plain 路径；已产出的 plain 结构化产物**不删**（只增不减），凭 provenance `"mode": "plain_structured"` 识别，开关关时渲染策略忽略它、走原 plain 渲染（views 传参给渲染策略，保持 layering）；FunASR 产物（无此标记）不受影响。

**完成标准 / 验收**：

- 开关关闭：plain 路径产物/渲染/章节 nolink 与现状一致（回归测试锁死）。
- 开关开启：
  - 断点不落在句子中间（仅停顿授权与硬切兜底两类例外；授权规则全集用性质测试锁定）；
  - `llm_processed.json` **序列化全文不含 "unknown"**（硬断言）；无 speaker 键、无 `speaker：`前缀；
  - SpeakerInferencer.infer 零调用（mock 断言）；key_info 在校准开启时仍提取（mock 断言）；
  - 集成测试：plain 源开关开启全链路 → `llm_processed.json` 落盘、渲染带 `dlg-{i}` 锚点、章节 fingerprint 匹配 → `jump_ok=1`；
  - 时间 None 段落照常渲染可跳（无 "00:00:00" 时间标签）；
  - 老 plain 缓存（无 segments）诚实走 PlainTextProcessor 降级路径，行为不变；
  - 下游逐消费者断言：recalibrate 姓名恢复对无 speaker 产物 no-op（mock 断言返回原对象）；`?raw=calibrated` 导出无 `：`前缀；audit/history 无 chapters 外新字段；开关关后重访开关开时的任务 → 渲染走 plain 策略（不显示陈旧段落）。
- T9 联动：3–5 个真实长转录（含访谈/对谈）跑开关开启路径，人工评估校对质量与段落读感；通过后再翻默认 true，并决定是否启动 v2 语义段落化（LLM 提议断点 + 本地吸附到合法位置，契约同 chapters starts-only，失败回退 v1）。
- 观测项（非完成标准）：校对 chunk 数/墙钟/token 与旧 plain 路径对比并记录，供分块参数调优。

**明确不做**：v2 语义段落化（待 T9 评估）；LLM 猜测说话人标签；老缓存迁移。

---

## T9 — 真实样本质量验收（P2）

3–5 个长转录本地跑 `ChaptersProcessor`，人工评切分与梗概后定稿 prompt。不阻塞 T6 接线，但上线前建议完成。

---

## T10 — 功能文档（P2）

- 新增 `docs/features/chapters.md`
- 更新 `docs/features/processing_options.md`（chapters 字段、默认跟随 summarize、分层矩阵）

---

## 派发 subagent 时粘贴模板

```text
你在 VideoTranscriptAPI 的 feat/chapters-foundation 分支上实现 【T?】。
全过程中文沟通，console 英文。遵守仓库 CLAUDE.md / AGENTS.md。

必读：
1. docs/plans/2026-07-16-chapter-outline-design.md（尤其 §5.1–5.7、§12）
2. docs/sessions/260719-0513-chapters/TASKS.md 中本任务整卡
3. 若改时间：只 import transcriber.segments

约束：TDD；小步 commit；不 push；改 CRLF 文件用 Edit。
先写失败测试，再实现，跑 uv sync --extra dev 后相关 pytest（看 exit code 或 junit，勿再加 -q）。
```

## 环境坑（摘自 HANDOFF）

1. 新 worktree 先 `uv sync --extra dev`  
2. pytest 已有 `-q`，勿再加 `-q`  
3. 时间工具唯一权威：`transcriber/segments.py`  
4. prompt 安全边界：`_flatten_for_prompt`  
5. `start_seg`/`end_seg` = 原始列表下标  
