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
