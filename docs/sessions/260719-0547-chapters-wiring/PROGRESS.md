# 接线批进度 — 260719-0547-chapters-wiring

> 更新：2026-07-19  
> 分支：`feat/chapters-wiring` @ worktree `.../worktrees/chapters-wiring`  
> 状态：**T1 + T6 + T7 完成；Review gate 通过；离线 e2e 冒烟通过**

## Commits（相对 `feat/chapters-foundation` / ddcac6c）

| Hash | Message |
|------|---------|
| `6df08bd` | 接通 CapsWriter/YouTube timeline 落盘与 get_cache 读回（T1） |
| `de21965` | processing_options: chapters 跟随 summarize 默认 |
| `63e1c50` | 缓存层支持 llm_chapters 与 chapters_status |
| `2a2c8c8` | 协调器与 llm_ops 接入章节生成与落盘 |
| `8bed090` | 分层 need_chapters 与 recalibrate 联动 |
| `e2ee221` | 兼容 dummy cache：chapters 梯度与 save_llm_status 入参 |
| `e497851` | 前端章节区块与 TOC XSS 加固（T7） |
| （docs） | session 进度 / backlog / 功能文档 |

## 任务状态

| 任务 | 状态 |
|------|------|
| T1 timeline 断链 | ✅ |
| T6 管线/状态/API/补层 | ✅ |
| T7 前端章节 + XSS | ✅ |
| T8 阶段二校对推广 | 未做（P2 独立开关，可另开 session） |
| T9 真实样本质量 | 未做（P2） |
| T10 功能文档 | ✅ 最小集（`docs/features/chapters.md` + processing_options 补充） |

## Review gate（独立 grok subagent）

| Round | 新增 P1 | 备注 |
|-------|---------|------|
| R1 | **无** | T1+T6；若干 P2 入 backlog |
| R2 | **无** | 含 T7；1 条新 P2（无 structured 时死链）入 backlog |

**Gate 条件满足**：连续 2 轮无新增 P1（未达 20 轮上限）。

## 测试

- `uv run pytest tests/unit`：T6 完成时 2329 passed  
- 关键子集：`test_chapters_pipeline_wiring` / `test_chapters_frontend` / `test_cache_timeline_wiring` / `test_processing_options`：144 passed  
- 离线 e2e 冒烟：3 条核心路径全部 OK（见 NOTES）

## 真机 e2e

已完成 YouTube 字幕 / FunASR 分层 / CapsWriter 真转录（见 NOTES）。死链修复已 commit。

## 延后

- **校对文本上的章节跳转（CW/YouTube）**：用户确认延后；说明见 `DEFERRED-chapter-jump-calibrated-text.md`。

## 未 push / 未合并 main

按 HANDOFF：全程不 push、不合并 main，除非用户明确授权。

**合并注意**：`feat/chapters-wiring` 叠在未 push 的 `feat/chapters-foundation` 之上；合 main 时通常 foundation+wiring 一起进，或先合 foundation 再合 wiring。
