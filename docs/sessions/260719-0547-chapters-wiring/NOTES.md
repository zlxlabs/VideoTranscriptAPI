# 接线批 NOTES / Backlog — 260719-0547

## 偏离计划的决策

1. **T7 提前在本 session 完成**  
   完成标准要求真机/离线 e2e，且内容默认公网可见，XSS 与章节渲染属公开展示路径；在 T1+T6 之后顺带完成 T7。

2. **协调器输入梯度**  
   本轮 structured dialogs 优先；否则用 llm_ops 预解析的 `timeline_segments`（cached dialogs / load_segments），避免双次 LLM。

3. **`create_task` 本地复算 chapters 默认跟随 summarize**  
   cache 层不反向 import api；与 `normalize_processing_options` 双源（P2 backlog）。

4. **指纹重算复用 `chapters_processor._compute_fingerprint`**  
   与生成侧 filter 口径一致；views 只认 GENERATED + 文件。

## Review backlog（接受不修，含理由）

| ID | 来源 | 项 | 理由 |
|----|------|----|------|
| B1 | R1-P2 | need_chapters/R6 测试是 mirror helper | 生产逻辑与镜像一致、无活 bug；后续可抽出共享函数再锁 |
| B2 | R1-P2 | `force_chapters_recompute` 与 suppress 互斥 | 现网 recalibrate 恒 `chapters=True` 掩盖；非当前路径数据丢失 |
| B3 | R1-P2 | `create_task` 重复 normalize | 行为当前一致；改耦合会动 cache/api 边界，优先减法 |
| B4 | R1-P2 | 离开 GENERATED 时旧 `llm_chapters.json` 残留 | T7 **仅认 status=GENERATED** 挡住公开展示；分层会重跑 |
| B5 | R1-P2 | T1 未断言 transcription 各 save_cache 点 | 契约测覆盖 save/load；调用点 mock 可后续补 |
| B6 | R1-P2 | `source.kind` 把 cached_dialogs 标成 segments | 可观测性，不影响生成正确性 |
| B7 | R2-P2 | 无 structured `#dlg-*` 时 fingerprint 命中仍发跳转 | 死链 UX，非 XSS/错误正文；阶段二/structured 路径正常。后续：仅当页面确有 dlg 锚点才 jump_ok |
| B8 | R2-P3 | TOC 章节样式类缺 CSS | 装饰性 |
| B9 | R2-P3 | 渲染侧 `int(True)==1` 对损坏 JSON | 生成侧已拒 bool |

## 离线 e2e 冒烟（完成标准）

在 worktree 内 `PYTHONPATH=src uv run python`：

1. **PATH1** — `normalize_processing_options` 跟随 summarize；仅 chapters 不触发 `_requires_llm_title`  
2. **PATH2** — CapsWriter `extra_json_data` → `transcript_capswriter.json` → `load_segments` / `get_cache["segments"]`  
3. **PATH3** — 写 `llm_chapters.json` + GENERATED；XSS 转义；fingerprint 去链接；FAILED 不渲染；`_save_llm_results` 落盘 roundtrip  

全部 PASSED。

## 契约速查（T7 依赖）

- 文件：`llm_chapters.json`  
- `start_seg` / `end_seg`：原始输入列表下标 → 锚点 `#dlg-{i}`  
- 仅 `chapters_status=GENERATED` 且文件存在才渲染  
- fingerprint 不一致：展示卡片，去掉跳转  
