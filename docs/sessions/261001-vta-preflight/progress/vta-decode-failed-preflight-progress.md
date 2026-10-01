# VTA decode_failed preflight progress

## 2026-10-01 21:24

- 里程碑：新增转录前媒体时长体检与规范化实现。
- 变更：`media_preflight.py` 使用 `ffprobe` 元数据探测；只有容器时长与音频帧估算偏差超过 2 秒时，才生成 16 kHz 单声道 FLAC。
- 验证：`uv run pytest -q tests/unit/test_capswriter_media_preflight.py`，10 passed。
- 清理：规范化失败时立即删除部分输出；成功返回后由调用方 `finally` 清理。
- 后续：把预检接入 `CapsWriterClient.transcribe_file` 的 SDK 调用，并核对现有测试桩。
