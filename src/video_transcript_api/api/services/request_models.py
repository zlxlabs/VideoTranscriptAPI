"""API request models shared by task submission and LLM reprocessing routes."""

from typing import Literal, Optional

from pydantic import BaseModel, Field, StrictBool, field_validator, model_validator

from ..processing_options import ProcessingOptions


class MetadataOverride(BaseModel):
    """Request fields that override parsed media metadata."""

    title: Optional[str] = Field(None, description="视频标题", max_length=200)
    description: Optional[str] = Field(None, description="视频描述", max_length=2000)
    author: Optional[str] = Field(None, description="视频作者", max_length=200)


class NotificationConfig(BaseModel):
    """Optional per-request channel and webhook selection."""

    channel: Optional[Literal["wechat", "feishu"]] = Field(
        None, description="通知渠道: wechat / feishu"
    )
    webhook: Optional[str] = Field(None, description="自定义 webhook URL")

    @model_validator(mode="after")
    def validate_channel_webhook_pair(self):
        if self.webhook and self.channel is None:
            raise ValueError("notification_config.webhook requires channel")
        return self

    @field_validator("webhook")
    @classmethod
    def validate_webhook_url(cls, value):
        if value is None or value.strip() == "":
            return value
        from ...utils.url_validator import URLValidationError, validate_url_safe
        try:
            validate_url_safe(value)
        except URLValidationError as exc:
            raise ValueError(f"webhook URL is not allowed: {exc}") from exc
        return value


class _WebhookRequest(BaseModel):
    wechat_webhook: Optional[str] = None
    feishu_webhook: Optional[str] = None
    notification_config: Optional[NotificationConfig] = None
    notify: Literal["default", "none"] = "default"

    @field_validator("wechat_webhook", "feishu_webhook")
    @classmethod
    def validate_webhook_url(cls, value):
        if value is None or value.strip() == "":
            return value
        from ...utils.url_validator import URLValidationError, validate_url_safe
        try:
            validate_url_safe(value)
        except URLValidationError as exc:
            raise ValueError(f"webhook URL is not allowed: {exc}") from exc
        return value


class TranscribeRequest(_WebhookRequest):
    """Video transcription submission request."""

    url: str = Field(..., description="视频URL（平台链接，用于 view_token 和缓存）")
    use_speaker_recognition: StrictBool = Field(False, description="是否使用说话人识别功能")
    download_url: Optional[str] = Field(
        None, description="实际下载地址（可选，如果提供则优先使用）"
    )
    metadata_override: Optional[MetadataOverride] = Field(
        None, description="元数据覆盖（用于补充或覆盖解析的元数据）"
    )
    processing_options: Optional[ProcessingOptions] = Field(
        None,
        description="处理深度开关（只转录/转录+校对/全流程）。None 等价于全部启用",
    )


class RecalibrateRequest(_WebhookRequest):
    """Request to recalibrate a previously transcribed task."""

    view_token: str = Field(..., description="查看页面的 view_token")


class ResummarizeRequest(_WebhookRequest):
    """Request to regenerate the summary for a previous task."""

    view_token: str = Field(..., description="查看页面的 view_token")


class GenerateNotesRequest(ResummarizeRequest):
    """Request to generate detailed notes for existing chapters."""
