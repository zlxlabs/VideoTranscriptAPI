# 进度：#159 音轨准入（audio-admission）

## 阶段

implementing → 自测完成，待主脑/独立审查（draft PR 已开，不自行 ready/merge）。

## 结论（本卡到目前为止的事实）

* 基线（本 worktree `7f085867`）`make test` 全绿（EXIT=0），所以本卡跑出来的任何
  新红都算本卡责任，无「继承红」可赖。
* 红证据（实现前）：无音轨纯视频经两条共享入口进 ASR 成功，`transcription.py`
  24 个新用例 AssertionError（含「任务成功但引擎已被调用」的假成功）。
* 实现后：`tests/unit/test_transcription_audio_admission.py` 25 条全绿；
  变异检验（把两处 `_ensure_audio_track(...)` 注入行换成 `pass`）→ 24 条转红，
  含`test_audio_admission_runs_once_before_asr`；还原注入行即恢复。
* 全量 `make test` 在修完 4 个受影响的既有测试文件后全绿（见「决定的否决」里
  的 scope 偏差记录）。

## 决定与否决

* **锁定（沿用design 7f085867）**：strict 准入，只成功确认 ≥1 条 audio 流才进
  ASR；探测失败一律任务失败，不放行、不假称无音轨。沿既有 `InvalidMediaError`
  的具名原因（`no_audio_track` / `media_probe_failed`）+ 既有
  `_fail_task_and_notify` + 既有 `finally` 清理，未新增结果类型/状态/缓存。
* **否决：新增通用媒体探测模块**（design 已否决，落地时同意）——两个引用点都在
  `transcription.py`，函数就地放（`_ensure_audio_track`），无第二消费者。
* **否决：probe 失败放行 / 三态结果对象**（design 上一稿方向，已撤回）——会把
  无法证伪的输入当合格输入。
* **否决：抽音轨**——音视频混合照常放行，只多一次 ffprobe（0.02–0.06 s，本地样本
  7–9 KB，见验证产物），不做 ffmpeg 转码；测试断言混合样本零 ffmpeg 调用。
* **否决：不收紧 `base.py:_validate_media_file`**——会让bilibili bbdown 预检在准入
  之前触发重试，形成两套规则（downloaders 不在本卡 scope 内，本次未改）。
* **新增决定（实现中发现）**：ffprobe 的 stderr 只以「首行、160 字符、ASCII
  转义」进日志，不进用户消息（`tests/unit/test_transcription_audio_admission.py::
  test_diagnostics_log_grepable_literals` 锁死）。
* **偏差（必须让主脑知情）**：卡面「允许」清单只列了 3 个既有测试文件，实际新准入
  还会打到另外 3 个**未列**的文件：`tests/integration/test_task_observability.py`、
  `tests/integration/test_temp_cleanup_integration.py`、
  `tests/unit/test_capswriter_deadline_budget.py`。三处都只加了**一行显式隔离的
  探测替身**（`monkeypatch.setattr(tx/module, "_ensure_audio_track", ...)`）并注明
  真值边界在准入专测文件里；没有改任何断言、没有全局关检查。这是必要的最小变更，
  否则 `make test` 会留 12 条新红。

## 下一步

1. 主脑错峰跑全量 `make test` +真实 CI（`gh pr checks` 看conclusion，不看颜色）。
2. 独立Codex 审查：重点看 strict 归因区分、两个准入点的位置、scope 偏差三处替身。
3. 本PR ready 前确认两个文档基线commit（`7f085867`、`3d27aa21`）以 merge commit
   进入 main，不许squash。