# 音轨最后测试增量独立审查

failure-visibility: clean

## 结论

**PASS，可交付。** 冻结增量 `27fc073f3828136471e35a9cfeac652aa900a6ec..e07d0eb1f8c4bedfbc756155a69d7061059db8f8` 闭合本卡列出的测试契约；运行时代码未改。三项已接受的 runtime P2 不在本轮重新评估。

## 专项四问

- 测试缺口：补齐三条 mixed 媒体路径的真实落盘/引擎入口字节哈希与事件顺序、API 探测失败通知、常规及 API 字幕直命中的零探测/零引擎断言。
- 新抽象：无生产抽象。已有 API 测试 fixture 增加 `subtitle` 输入，用于同一 API 路由的第二种输出形态。
- 新状态/fallback：运行时无新增状态或 fallback。
- 双路径：运行时无新增或残留双路径；只扩展既有路径的测试。

## 列明不变式与证据

1. **mixed 字节及探测时序闭合。** 常规 `download_file`：`tests/unit/test_transcription_audio_admission.py:683`；YoutubeAPI：`:633`；priority：`:844`；共享断言 `:712` 验证下载 hash、引擎入口 hash、engine path、真实 probe path，以及 `download→probe→engine`。真实 subprocess 事件记录在 `:443-466`。在真实 ffprobe 返回后仅给媒体追加一个 NUL 字节，三项各自都在 `pre_digest == engine_digest` 处 `AssertionError`，该 pytest 命令退出码 1。
2. **API probe 失败走 API 路径并通知失败。** `:1082-1106` 断言 producer 为 `fetch_for_transcription`、终态 failed、payload 含 `media_probe_failed` 且不含 `no_audio_track`、无成功缓存、双 engine 零调用；`:511-516` 同时检查 FAILED 通知和持久终态。测试侧屏蔽终态通知后，该测试在 `no terminal failure notification` 处失败，退出码 1。
3. **两种字幕直命中不探测、不转录。** 常规字幕：`tests/features/test_transcription_flow_regression.py:471-519`，成功文本/缓存、零 admission、零真实 ffprobe，两个 engine 构造器均为 fail-closed guard。API 字幕：`tests/unit/test_transcription_audio_admission.py:871-902`，成功文本/缓存、无媒体路径、producer 路由命中、零 ffprobe/子进程/engine。API fixture 在 `:330-344` 以精确 `YoutubeDownloader` 类创建并以非空 client backing `use_api_server`；`:519-528` 锁定精确类名、property 和 backing。测试侧强行构造/调用 ASR 后，两项字幕测试分别在构造 guard 和 `_engine_calls() == []` 断言失败，退出码 1。
4. **既有契约未削弱。** 真假媒体及常规/API 分支仍由 `:549-625` 覆盖；foreign task 与既有 cache 文件所有权清理断言仍在 `:557-594`；真实 ffprobe argv/JSON producer 断言仍在 `:918-956`。冻结增量 `src/` diff 为空。
5. **目标及全量验证通过。** 两份目标文件整文件运行：72 passed、78 warnings、0 skipped，退出码 0。合成树父提交为 frozen `e07d0eb1f8c4bedfbc756155a69d7061059db8f8` 与 main `5bb58f923bbd185a01cc20a48f7aab7e2b8075e1`，tree `c43913285f4b371e32b15057aba4e476059461ef`；其中唯一一次 `make test` 到 100%，退出码 0。全量运行有 3 个 skip 标记，但两份目标文件 0 skipped，故本增量没有新增 skip。

## OCR、红灯与恢复

OCR 前置 envelope 完整且 JSON 可解析：`status=skipped`、`reason=no_reviewable_items`、`findings=[]`、`coverage=none`、`cli_status=skipped`；冻结差异中只有测试和 progress 项，没有 OCR 可审的运行时代码。按 skipped 记录，不视作 clean，也不替代本审查。

主干基线 API 在派发时不可用；**继承红未能判定**。本次合成树全量 `make test` 无新红（退出码 0）。

所有临时故障注入已逐字节恢复。当前两份测试文件 SHA-256 分别为 `fbc41acfc80f43623096f3b56565887a3308f281a66136fd69b504ad01cc6542` 与 `0467ee1cc1981761800c0eec068bbe9066fb924f4d087ece91384dd12beb6a37`；各自 Git blob 与 HEAD、index 一致。最终 `git diff HEAD -- src tests` 和 index 对应 diff 均为空。
