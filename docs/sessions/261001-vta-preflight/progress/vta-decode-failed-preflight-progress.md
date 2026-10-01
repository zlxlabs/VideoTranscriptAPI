# VTA decode_failed preflight progress

## 2026-10-01 21:24

- 里程碑：新增转录前媒体时长体检与规范化实现。
- 变更：`media_preflight.py` 使用 `ffprobe` 元数据探测；只有容器时长与音频帧估算偏差超过 2 秒时，才生成 16 kHz 单声道 FLAC。
- 验证：`uv run pytest -q tests/unit/test_capswriter_media_preflight.py`，10 passed。
- 清理：规范化失败时立即删除部分输出；成功返回后由调用方 `finally` 清理。
- 后续：把预检接入 `CapsWriterClient.transcribe_file` 的 SDK 调用，并核对现有测试桩。

## 2026-10-01 21:27

- 里程碑：接入 `CapsWriterClient.transcribe_file`。
- 变更：SDK 始终接收预检返回的路径，但结果侧车仍以原始媒体路径命名；`AsrError.retryable` 重试判断保持不变。
- 清理：SDK 成功、不可重试失败和 `KeyboardInterrupt` 异常路径均在 `finally` 删除规范化文件。
- 验证：媒体体检单测 10 passed；CapsWriter 回归单测 7 passed。
- 后续：补齐真实 ffmpeg fixture 取证、反向红验、静态检查并运行全量门禁。

## 2026-10-01 21:35

- 里程碑：完成真实 ffmpeg fixture 复核并修正探测判据。
- 发现：仅按 AAC `nb_frames` 推算时长会误伤带异常负时间戳的合法解码结果；改为读取声明尾部的音频包 JSON，使用最后包的 `pts_time + duration_time`。
- 取证：重复时间戳直播流 fixture 的 `ffprobe format.duration=4.806522`，原始 `ffmpeg -ar 16000 -ac 1 -f f32le` 解码样本数为 `241859`；规范化 FLAC 的 `ffprobe duration=15.116188`，解码样本数仍为 `241859`。
- 清理：规范化文件在清理前出现在临时目录，清理后目录只剩原始 fixture。
- 后续：执行反向红验、ASCII 字符串检查、相关测试和 `make test`。

## 2026-10-01 21:38

- 里程碑：完成收尾验证。
- 反向红验：一致时零转码、偏差时规范化两条测试均在最小判据注入后以 `AssertionError` 失败，随后只还原注入行。
- 相关测试：卡面列出的 9 个测试文件共 `41 passed`。
- 全量门禁：`make test` 退出码 0；pytest 进度核对为 `3379 passed, 0 skipped in 78.439s`。
- 静态检查：相关 Python 文件 `compileall` 通过；新增源文件与测试文件 ASCII-only。
## 2026-10-01 21:56

- 里程碑：R1 返工，废弃尾部包探测，改用 AAC `nb_frames * 1024 / sample_rate`。
- 变更：测量不可得时走原路径并打 `Media preflight sample count unavailable` 日志；ffprobe 崩溃/JSON 不可解析仍 fail fast；`transcribe_file` 把体检纳入 try/finally，错误带媒体路径。
- 验证：待跑单测与真实 fixture。
