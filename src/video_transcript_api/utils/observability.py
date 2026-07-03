"""可观测性接入（GlitchTip 错误上报）。

对接舰队 D5 体系的 zlx-ops-sdk，向 GlitchTip 上报未捕获异常。

设计约束（与舰队 playbook 一致）：
- **全程 fail-open**：SDK 缺失、DSN 未配置、init 抛错，都不得影响主服务启动。
  没配 SENTRY_DSN = no-op，不报错。
- **DSN 只从环境变量读**（由 docker-compose 的 env_file: ops.env 注入 os.environ），
  绝不写进 config.jsonc / 仓库，避免明文泄露。
- **release 取 GIT_SHA**：Dockerfile 在 runtime 阶段注入，GlitchTip 据此按版本聚合错误。
"""

from __future__ import annotations

import os

from .logging import logger

# GlitchTip 中的服务名与仓库标识，供错误聚合与跳转源码使用
_SERVICE_NAME = "VideoTranscriptAPI"
_REPO = "zj1123581321/VideoTranscriptAPI"


def init_observability() -> None:
    """初始化错误上报。fail-open：任何异常仅记日志，绝不向上抛。"""
    dsn = os.environ.get("SENTRY_DSN")
    if not dsn:
        # 未配置 DSN 属正常情况（本地开发 / 未接监控的环境），静默跳过
        logger.info("observability: SENTRY_DSN 未设置，跳过错误上报接入")
        return

    try:
        import zlx_ops_sdk

        # dsn 缺省即读 env SENTRY_DSN；release 缺省取 env GIT_SHA 拼 repo@<sha>
        zlx_ops_sdk.init(_SERVICE_NAME, repo=_REPO)
        logger.info("observability: GlitchTip 错误上报已接入 (release={})",
                    os.environ.get("GIT_SHA", "unknown"))
    except Exception as exc:  # noqa: BLE001 - fail-open，绝不因可观测拖垮主服务
        logger.warning("observability: 初始化失败，已降级为 no-op: {}", exc)
