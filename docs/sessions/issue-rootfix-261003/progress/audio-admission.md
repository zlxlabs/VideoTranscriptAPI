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
---

## 阶段（第二轮 · 修复卡）

repairing → 测试不变式补齐完成，保持 draft，待新的独立增量 review。
基线 `bf488aff`（运行时代码**一行未改**）。

## 结论：上一轮的两条「已覆盖」是假阳性，本轮已纠正

独立验收（`reviews/audio-verdict.md`，P2 only）指出我上一轮的覆盖声明不成立，现已实跑证实：

* **YouTube API 分支从未被走到**。生产判定是精确类名
  `metadata_downloader.__class__.__name__ == "YoutubeDownloader"`（`transcription.py:1883`）
  且 `hasattr(use_api_server)` 且为真值；上一轮替身叫 `YoutubeApiServerDownloader`
  → 类名不匹配 → 静默走常规下载路线，测的其实是**另一个**准入点。
  RED 证据（先加断言后修 fixture）：
  `AssertionError: assert 'YoutubeApiServerDownloader' == 'YoutubeDownloader'`。
* **YouTube 优先下载分支从未被走到**。生产条件是
  `hasattr(download_video_with_priority)` 且 URL 含 youtube.com
  （`transcription.py:2384`）；上一轮 `RouteDownloader` 根本没有这个方法，
  `route="youtube"` 只是把普通 `download_file` 跑了一遍。
* **修复后移除 API 分支那一行准入 → 4 条转红**（原先的假阳性全部暴露）：
  `test_no_audio_track_is_rejected_at_youtube_api_entry`（首红断言就是 engine-zero：
  `AssertionError: assert ['/tmp/.../task_t-noaudio-youtube-api/api_audio.m4a'] == []`）、
  `test_youtube_api_audio_only_reaches_engine`、
  `test_probe_failure_at_youtube_api_entry_is_check_failure`、
  `test_mixed_media_bytes_are_unchanged_across_admission`。
  精确恢复后源码 SHA-256 = `72180f32419e79e47c0b48bf0f1abe8a74540e9b182fb03a0adea6b15f4a3b7e`
  （与独立验收记录的 H0 一致），`git diff HEAD -- src/` 为空。
* 另测：移除**常规**那一行准入 → 22 条转红，含 3 条优先下载用例 → 两个闸都有约束力。
* **补上媒体字节不变断言**：mixed 实际 task 入口路径 + API 分支 + 优先分支，
  准入前（下载落地那一刻）与引擎入口的 sha256 由真实文件字节分别现算，必须相等，
  并用 lavfi 样本源文件哈希作第三个独立锥点；`EVENTS` 断言取样顺序
  `["download", "engine"]`，排除「两次都在准入之后」。

## 决定与否决（本轮）

* **替身用生产真实类 + 实例级桩**（`YoutubeDownloader.__new__(YoutubeDownloader)`），
  **否决继承子类**：子类会让 `__class__.__name__` 判定静默失效，正是上一轮假阳性的根因。
* `use_api_server` 是生产只读 property（`_youtube_api_client is not None`），
  因此设的是它真正的后端，不是替身另开的同名属性。
* **新增**：优先下载分支 3 条用例（无音轨拒 / 探测失败归因 / 含音轨放行对照 + 字节不变）。
* **否决（不做）**：不改任何 src（P2-2 畸形 `codec_type`、P2-3 其它 OSError、
  P2-4 stderr 原文日志一律登记不修，主脑在 PR 正文明示接受）；不新增接口/开关/fixture 框架。
* **保留**：上一轮 3 处清单外 fixture 最小 19 行 —— 主脑亲读增量后**显式批准纳入本卡范围**
  （此前偏差作为事实保留记账，不说成先前已授权）；原有 foreign 文件/缓存产物清理断言不动。

## 下一步

1. 新的独立增量 review（不复用上一轮 reviewer）。
2. 全量 `make test` 与正式 CI 由主脑接续；ready 之后主审才会真跑（draft 期primary 是 SKIPPED）。
3. 合并仍须 merge commit 保留 `3d27aa21`、`7f085867`。
