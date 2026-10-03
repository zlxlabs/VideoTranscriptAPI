# Audio admission increment verdict

failure-visibility: p2-only

- **审查范围**：冻结 H0 `bf488affcef6c2ecd91f6c05a0539947bd671e32` → H1 `27fc073f3828136471e35a9cfeac652aa900a6ec`；风险 personal；关联 issue #159。
- **判定：不通过，关键入口/哈希验收未全部闭合。** API 与 YouTube 优先下载的真实分支选择，以及两处准入 gate 的逆向变异红验已闭合；当前运行时源码未变。缺口是哈希测试没有普通下载路径且没有显式准入事件、API 探测失败测试没有断言 FAILED 通知、字幕直命中没有锁死零 ASR/零探测。不要把本行 P2-only 解读为验收通过。
- **源码不变**：`git diff H0 H1 -- src/video_transcript_api` 为空。生产文件 `src/video_transcript_api/api/services/transcription.py` 的 H1 SHA-256 为 `72180f32419e79e47c0b48bf0f1abe8a74540e9b182fb03a0adea6b15f4a3b7e`；每个临时变异之后均恢复到该 SHA，最终 H1 源码 diff 为空。

## H0..H1 专项增量四问

1. **是否只补登记测试缺口：基本符合代码边界，进度文本不作内容审查。** 代码增量只有 `tests/unit/test_transcription_audio_admission.py`；新用例针对卡面登记的真实 YouTube API 类型条件、优先下载 producer、入口准入及媒体字节不变。另有 `docs/sessions/issue-rootfix-261003/progress/audio-admission.md` 变化（53 行新增、1 行删除）；依输入隔离要求没有打开它，故不以其内容判断实现理由或扩展范围。
2. **是否新增未经批准的抽象：没有生产抽象。** 新增的 `YoutubeRouteRecorder`、`_real_youtube_downloader`、`_attach_common_stubs` 都是测试夹具；两种 YouTube fixture 共用同一构造与记录器。`file_digest`/`EVENTS` 只记录真实测试文件字节与事件，不进入运行时。
3. **是否增加无依据状态或 fallback：没有。** 生产源码与 H0 相同；没有生产状态、fallback、重试或配置变化。`EVENTS` 是测试过程中的观测列表，由 fixture 每次清空。
4. **是否留下双路径：没有运行时双路径。** 测试分别覆盖原有 API 快速路径与原有 YouTube 优先下载路径；没有复制或替代生产准入逻辑。

## 关键不变式与证据

- **API 精确分支条件与 producer**：生产入口在 `transcription.py:1881-1887` 检查精确类名 `YoutubeDownloader`、只读 `use_api_server` 属性及其真值。真实类的 property 在 `downloaders/youtube.py:69` 读取 `_youtube_api_client is not None`。测试用 `YoutubeDownloader.__new__` 构造该类并设置其实际 backing field（`test_transcription_audio_admission.py:330-344`）；API 入口用例在 `:579-612` 断言 `fetch_for_transcription` 被调用、`download_file` 与 `get_download_info` 未调用，且断言失败终态和双引擎零调用。API 有音轨对照在 `:614-634` 到达 FunASR。
- **优先下载分支与终态**：生产选择条件在 `transcription.py:2384-2403`。无音轨、探测失败、含音轨对照分别在 `test_transcription_audio_admission.py:750-775`、`:778-799`、`:802-823`；都断言真实入口记录器实际收到 `download_video_with_priority` 且没有 `download_file`。两种失败分别断言 `no_audio_track`/`media_probe_failed`、FAILED 通知和引擎零调用；有音轨对照进入 FunASR。
- **共享 gate**：API ASR 前的 gate 位于 `transcription.py:2022-2034`；普通下载、预下载及 YouTube 优先路径共用的 gate 位于 `:2417-2428`。API 无音轨测试及普通入口参数化用例都锁定失败终态、通知、缓存和引擎行为；普通入口 also 保留 foreign task 文件和已有缓存产物的清理边界（测试 `:531-577`）。现存 `tests/features/test_transcription_flow_regression.py:495-526` 用明确事件序列锁定常规准入发生在 ASR 前且恰好一次。
- **逆向变异（全部恢复）**：①只把 API gate 调用替换为无操作，确认注入行后运行 `test_no_audio_track_is_rejected_at_youtube_api_entry`，退出 1，`assert _engine_calls() == []` 触发 `AssertionError`（日志 `/tmp/audio-increment-api_gate_removed.log`）。②只替换普通 gate，普通入口的 4 个参数化用例全部退出 1，终态成功值触发 `AssertionError`（`/tmp/audio-increment-regular_gate_removed.log`）。③在 `_ensure_audio_track` 成功后追加字节，API 音轨用例在 `pre_digest == engine_digest` 处退出 1（`/tmp/audio-increment-post-admission-bytes.log`）。测试源文件未改；生产源均恢复到上述 H1 SHA。
- **SHA-256 与时间顺序：未完全闭合。** `_assert_bytes_unchanged` 在 `test_transcription_audio_admission.py:683-702` 对 producer 实际落盘和 engine 实际读取的字节做 SHA-256 比较；API 与优先路径调用它（`:705-722`、`:802-823`），准入后字节变异会使比较转红。但普通下载对照 `test_mixed_media_reaches_funasr_without_transcoding`（`:661-680`）没有哈希断言，`:705-722` 的 `test_mixed_media_bytes_are_unchanged_across_admission` 实际使用 API fixture。`EVENTS` 只记录 `download`、`engine` 两种事件，`:700-701` 仅断言这两个阶段；没有记录准入事件，故不能用该事件序列证明哈希采样严格早于 gate。代码当前调用顺序可由 producer 与 gate 的源码位置读出，但卡面要求测试事件本身证明顺序。
- **真实 ffprobe producer**：`test_transcription_audio_admission.py:839-920` 通过真实 `ffmpeg -f lavfi` 生成样本，并由 `SubprocessSpy` 调用实际 `ffprobe`，断言 argv、JSON、探测次数及无 ffmpeg 抽轨。目标测试文件本轮 30 项全部通过，条件跳过数为 0；本机实际二进制为 `/usr/bin/ffmpeg` 和 `/usr/bin/ffprobe`。
- **字幕直命中与 helper 边界：测试未锁全。** API 字幕命中分支 `transcription.py:1914-1963` 在 API gate（`:2027`）之前直接处理字幕；现有标准字幕测试 `tests/features/test_transcription_flow_regression.py:471-492` 只断言结果文本和缓存，没有断言 `_ensure_audio_track`、FunASR、CapsWriter 都未调用，也没有 API 字幕快路径 fixture。`_ensure_audio_track`（`transcription.py:66-159`）只读文件并启动 ffprobe，不写媒体、不抽轨；两处真实 ASR 路径在对应 gate 后。该运行时事实不替代缺失的负向测试断言。

## 发现与风险处置

- **P2 新发现：哈希入口与采样时序断言不足**，违反卡面不变式 4。普通下载路径缺哈希比较，哈希测试也没有 gate 事件。必须明确判为未闭合。
- **P2 新发现：API 探测失败通知未被断言**，违反卡面不变式 1。`test_probe_failure_at_youtube_api_entry_is_check_failure`（`test_transcription_audio_admission.py:1003-1024`）断言结果为 failed、消息为 `media_probe_failed`、引擎零调用，但没有调用 `_terminal_failure` 校验 FAILED 通知；API 无音轨用例有该断言，不能替代探测失败情形。
- **P2 新发现：字幕直命中缺零调用断言**，违反卡面不变式 5。当前源码分支顺序正确，但相关测试不能在新增 ASR/探测调用时保证转红。
- **预算偏差**：H0..H1 总计 382 行新增、40 行删除（422 行变更），超过 Diff-Lines-Hard 300；测试文件单独新增 329 行。目标 150 也明显超出。作为 reviewer 记录为任务卡预算不符合；本轮没有扩展或修改实现范围。
- **已接受且不要求修复的运行时 P2**：probe stream 缺少 `codec_type` 时会归为 `no_audio_track`；除 `FileNotFoundError`/超时外的其他 `OSError` 未统一归一化；stderr 摘要直接取首行而不是白名单。三项均按卡面接受不修，不建议为其加机制。
- **运行时 P1：无。** 没有新的运行时差异或真实 P1 证据。外部 OCR 没有审查到任何项，见下方三态；人工审查仍完整执行。

## OCR 与测试结果

- **OCR：`skipped`**，envelope `reason=no_reviewable_items`、`coverage=none`、`findings=[]`。这是未审查，不是干净结论；手工 review 不受其替代。
- **目标测试文件**：`uv run --frozen pytest -v -ra tests/unit/test_transcription_audio_admission.py`，退出 0：**30 passed, 78 warnings, 2.66s**，没有 skip。
- **H1 完整入口**：`make test` 在审查树退出 0，pytest 进度到 100%，观察到 3 个 skip，无失败。入口使用 `-q`，未打印 collected 总数/skip 原因；本结论来自这次真实完整执行，不使用缓存点数代替。
- **当前主干合成树完整入口**：在 scratch worktree 临时合并 H1 与 `5bb58f923bbd185a01cc20a48f7aab7e2b8075e1`，父提交依次为 `27fc073f3828136471e35a9cfeac652aa900a6ec` 和 `5bb58f923bbd185a01cc20a48f7aab7e2b8075e1`，合成 tree `b64bb17851b4cefa29e54f7cc9da216955d9ae59`。合并无冲突，`make test` 退出 0、进度 100%、3 个 skip、无失败。scratch-worktree 已自动移除；审查树仍在 H1。
- **CI 红归因**：派发卡记录的主干基线为 `gh api request failed`。本地两次完整运行无红；继承红无法判定，新增红为无。三项预期变异红已单独记录，不计作套件失败。

## 收尾状态

- OCR 状态：skipped；人工 review 已完成，结论不通过，待上述测试缺口补齐后重审。
- H1 源码保持冻结字节；当前审查树唯一允许的持久改动是本 verdict 文件。
- 没有 ready、合并或部署实现 PR；本卡只提交并推送 verdict。
