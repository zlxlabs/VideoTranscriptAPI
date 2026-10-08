# 部署验证回执

- 合并提交：`699689b84afb5c4ae44e49742b6bd90b475c51b7`
- 构建版本：`699689b84afb`
- 镜像 digest（脱敏，前 12 / 后 8）：`sha256:989731f5d192…cbe99d27`
- 部署时间：`2026-10-08T06:16:48Z`；验证时间：`2026-10-08T06:20:45Z`
- 部署健康：容器 `healthy`；`/livez` HTTP 200；只读健康端点 HTTP 200；重启计数在 10 秒复核期间稳定为 0。
- 新代码验证：运行镜像 `GIT_SHA` 与合并版本一致；`completion_share.py` 存在；`transcription.py` 的 `completion_receipt` 转发验证通过（出现 3 次）。
- 真实视频验证：未进行；未提交视频，也未发送 Webhook 测试消息。
