"""Notification fields for local-upload tasks; only delivery payloads get enriched."""

import json


LOCAL_UPLOAD_SOURCE_LABEL = "本地上传"


def local_upload_notification_fields(upload: dict) -> dict:
    """Build safe user-facing fields without exposing server-owned media paths."""
    metadata = json.loads(upload.get("request_metadata") or "{}")
    title = (
        metadata.get("title")
        or upload.get("title")
        or upload.get("filename")
        or LOCAL_UPLOAD_SOURCE_LABEL
    )
    return {
        "title": title,
        "source_label": LOCAL_UPLOAD_SOURCE_LABEL,
        "source_url": upload.get("source_url") or "",
    }
