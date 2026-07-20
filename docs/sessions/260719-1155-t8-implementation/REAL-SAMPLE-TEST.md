# T8 真实样本实测报告（本地环境）

> 日期：2026-07-19
> 环境：本地 worktree（feat/chapters-wiring，HEAD=e609a4f），`uv run python main.py --start`，:8000
> 数据源：用户指定的 3 个历史视频（生产环境未动，全部在本地重建/复用）
> 后端：youtube_api_server / CapsWriter / FunASR 均真实可达；LLM 真实调用（deepseek-chat）

## 样本与结论总览

| 样本 | 类型 | 结论 |
|------|------|------|
| Terence Tao – How the world's top mathematician uses AI（YouTube，84min，英文自动字幕） | plain，新转录 + recalibrate | **全链路符合预期**（唯一发现的阻塞为 chapters 校验 bug，已修） |
| 【巫师】韩国再拿第一，工蜂经济倒逼年轻人选择双输（bilibili BV18QLD6eEYz，13min，CapsWriter ASR） | plain，calibrate=false 首跑 + recalibrate | **符合预期**；首跑章节 skipped_short 系本地阈值默认 10000（转录 ~8000 字）——对齐生产 `min_chapters_threshold=1000` 后 recalibrate 生成 5 章且全部 jump_ok=1（见「追加：章节阈值」）；ASR 片尾乱码校准未完全救回（源质量问题） |
| 别给人类写软件了（小宇宙，FunASR speaker=1 历史任务） | speaker 回归样本 | **开关 ON/OFF 下渲染与基线逐点一致**（145 锚点、说话人姓名不变） |

## 开关 ON 全链路验收（Terence）

- 转录产生 segments 侧车 `transcript_capswriter.json`（2338 段，float 秒）。
- recalibrate（开关 ON）→ `llm_processed.json` 落盘：
  - 顶层 `mode == "plain_structured"` ✓；150 段落（2338 段 → 150 段）；
  - dialogs 无 speaker/speaker_id 键 ✓；`speaker_mapping == {}` ✓；
  - 剔除 calibration_stats 后序列化全文 0 处 "unknown" ✓（stats 内 `unknown_id: 0`）；
  - 校准 28 chunk 全成功（applied 2298 / kept 40 / malformed 6），calibration_status=full。
- `llm_calibrated.txt` 无前缀变体 ✓（无 `xxx：` 行首）。
- 章节：`llm_chapters.json` 12 章，`source.kind == "dialogs"`（取本轮段落化产物）✓。
- 查看页：150 个 `dlg-{i}` 锚点；**12 章全部 `data-jump-ok="1"`**；12 个 `href="#dlg-*"` 全部有落点 ✓。
- 段落质量：断点末字符分布 `.`×104、`,`×34、`?`×4、硬切 7 处；>hard_max(600) 53/150（英文句长 + 自动字幕无标点长句所致，最长 1227 ≈ 2×hard_max 界内，与 R2 fuzz 结论一致）；7 处硬切全部落段边界（终端规则 warning 已记日志）。
- 巫师：17 段落全部 ≤402 字符，**16/16 非末段以 "。" 收尾**（纯标点授权），时间轴连续。

## 开关 OFF 回退（方案 b）

- Terence 查看页：0 dlg 锚点（plain_structured 产物被忽略，走 plain 渲染）✓；
- 12 章仍展示但全部 `data-jump-ok="0"`（锚点源回退后指纹不匹配 → nolink）✓；
- 巫师查看页：0 dlg 锚点 ✓；
- 产物只增不减：llm_processed.json 保留未删 ✓。

## 发现并修复的阻塞（已提交 e609a4f）

**chapters `_validate_and_normalize_start_segs` 严格 isinstance int 导致真实模型输出失败**：
deepseek-chat 把靠后章节的 start_seg 输出为字符串 `'1676'`，两次尝试均判 semantic
validation failed → chapters_status=failed（开关 OFF 首跑即暴露，与 T8 无关的既有
robustness 缺口，但阻塞 jump_ok 验收）。修复：整数值字符串/浮点强转 int（bool、
非数值仍拒绝），+5 单测（tests/unit/test_chapters_processor.py
TestStartSegTypeCoercion）。修复后 chapters 一次成功（20,644 tokens / 15s，
此前失败两次烧掉 84,352 tokens）。

## 观测项（供 T9 调参，非验收标准）

| 指标 | Terence 开关 OFF（旧 plain） | Terence 开关 ON（T8） |
|------|------------------------------|------------------------|
| 校准 LLM 调用 | 54 次 | 28 次（28 chunk） |
| 校准 tokens | 69,843 | 126,814 |
| 校准墙钟 | 212s | 287s |
| 章节 tokens | 84,352（2 次均失败） | 20,644（1 次成功） |

- 结构化逐段校对 token 约为旧路径 1.8×（id 行格式 + 逐 chunk 指令开销），墙钟 +35%；
  章节 prompt 因输入从 2338 段变 150 段而显著变小。
- 巫师 recalibrate：校准 3 次调用 11,875 tokens / 31s；总结补跑 6,864 tokens / 30s。

## 追加：章节阈值与小宇宙任务无章节的原因（2026-07-19 晚）

用户反馈三个样本里只有 Terence 有章节。排查结论：

1. **巫师**：首跑 `chapters_status=skipped_short`——本地 config 未配
   `min_chapters_threshold`，代码默认 10000 字符，其转录全文 ~8000 字被诚实跳过。
   生产/worktree 本地配置为 1000。对齐本地配置为 1000 后 recalibrate：
   `chapters_status=generated`，5 章（`source.kind=dialogs`，17 段落），
   查看页 17 个 dlg 锚点、**5/5 `data-jump-ok="1"`**。
   教训：**测试环境的章节阈值要与生产对齐**，否则"没章节"是配置差异而非功能缺陷。
2. **小宇宙（杨攀）**：历史 FunASR 任务，处理时分支还没有 chapters 功能，
   缓存里无 `llm_chapters.json`/无 `llm_status.json`，查看页不会追溯生成。
   要补章节需 recalibrate（会重跑整轮 FunASR 校准 + 说话人推断，改动历史产物），
   本轮未动——它继续作为 speaker 路径回归基线。

## 遗留与备注

- 巫师 ASR 片尾乱码（"同单单是是…"）校准未完全救回——源转录质量问题，非 T8 机制问题。
- 英文 YouTube 段落偏粗（中位 516 字符）：停顿授权在无缝滚动字幕上几乎不触发，
  逗号级兜底用了 34 次；如 T9 读感评估认为偏粗，可下调 target_chars 或启用 v2。
- 本地 config.jsonc 变更（未入库）：补 `concurrent.llm_max_workers`、
  `storage.workspace_dir`、删 `capswriter.path`（新校验器要求）；
  `structured_calibration_for_plain: true` + paragraphization 段（**测试完当前保持 ON，
  服务器 :8000 运行中供查看**；关回即恢复暗启动默认）。
- 备份：/tmp/t8-test-backup/（cache.db.bak、小宇宙缓存、各阶段 HTML 快照）。
- 本地查看入口：
  - Terence：http://100.87.124.57:8000/view/view_tgKLtQwW_Yyca9WoQEjM5YO2QL9QWeSfKtQ5XcHgkqY
  - 巫师：http://100.87.124.57:8000/view/view_V1pKWEcCNMsW47TpltcoaGNzDXrZyreIuT_Gy6_-ssg
  - 小宇宙：http://100.87.124.57:8000/view/view_TUVgkd46P7Q-UGVA9BvkR6RhyBNseK9XwfK-jre2TUI
