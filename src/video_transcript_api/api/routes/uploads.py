"""Bounded raw-byte local upload API."""

import asyncio
import base64
import binascii
import hashlib
import json
import math
import os
import re
import uuid
from pathlib import PurePosixPath
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import JSONResponse

from ..context import (
    ApiError,
    get_cache_manager,
    get_config,
    get_inflight_registry,
    get_task_queue,
    get_temp_manager,
)
from ..processing_options import normalize_processing_options
from ..services.transcription import NotificationConfig, verify_token
from ...utils.logging import logger
from ...utils.notifications import get_notification_router
from ...utils.notifications.targets import resolve_notify_targets

router = APIRouter(prefix="/api/uploads", tags=["uploads"])
_METADATA_MAX_BYTES = 4096
_BASE64URL = re.compile(r"^[A-Za-z0-9_-]+={0,2}$")
_REQUIRED_METADATA = {
    "filename", "byte_size", "title", "source_url", "retention", "processing_options"
}
_OPTIONAL_NOTIFICATION_METADATA = {
    "notify", "wechat_webhook", "feishu_webhook", "notification_config"
}
_ALLOWED_METADATA = _REQUIRED_METADATA | _OPTIONAL_NOTIFICATION_METADATA
_MIB = 1024 * 1024


def decode_upload_metadata(value: str) -> dict:
    """Decode and validate the bounded UTF-8 JSON metadata header."""
    if not isinstance(value, str) or not value or len(value) > 6000 or not _BASE64URL.fullmatch(value):
        raise ValueError("X-Upload-Metadata must be bounded base64url")
    try:
        encoded = value.rstrip("=")
        raw = base64.b64decode(
            encoded + "=" * (-len(encoded) % 4), altchars=b"-_", validate=True
        )
        if len(raw) > _METADATA_MAX_BYTES:
            raise ValueError("upload metadata exceeds 4096 bytes")
        data = json.loads(raw.decode("utf-8"))
    except (binascii.Error, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("X-Upload-Metadata must encode UTF-8 JSON") from exc
    if (
        not isinstance(data, dict)
        or not _REQUIRED_METADATA <= set(data)
        or set(data) - _ALLOWED_METADATA
    ):
        raise ValueError("upload metadata fields are incomplete or unsupported")

    filename = data["filename"]
    if not isinstance(filename, str) or not filename.strip():
        raise ValueError("filename must be non-empty text")
    if len(filename.encode("utf-8")) > 255:
        raise ValueError("filename exceeds 255 UTF-8 bytes")
    if any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in filename):
        raise ValueError("filename contains control characters")
    display_name = PurePosixPath(filename.replace("\\", "/")).name
    if not display_name or display_name in {".", ".."}:
        raise ValueError("filename has no display basename")

    byte_size = data["byte_size"]
    if not isinstance(byte_size, int) or isinstance(byte_size, bool) or byte_size <= 0:
        raise ValueError("byte_size must be a positive integer")
    title = data["title"]
    if title is not None and (
        not isinstance(title, str) or len(title) > 200
        or any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in title)
    ):
        raise ValueError("title must be at most 200 characters")
    source_url = data["source_url"]
    if source_url is not None:
        if not isinstance(source_url, str) or len(source_url.encode("utf-8")) > 2048:
            raise ValueError("source_url exceeds 2048 UTF-8 bytes")
        parsed = urlsplit(source_url)
        if (
            parsed.scheme.lower() not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in source_url)
        ):
            raise ValueError("source_url must be http(s) without userinfo")
    retention = data["retention"]
    if retention not in {"30d", "never"}:
        raise ValueError("retention must be '30d' or 'never'")
    options = data["processing_options"]
    if not isinstance(options, dict):
        raise ValueError("processing_options must be an object")
    try:
        normalized_options = normalize_processing_options(options)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid processing_options: {exc}") from exc

    notify = data.get("notify", "default")
    if not isinstance(notify, str) or notify not in {"default", "none"}:
        raise ValueError("notify must be 'default' or 'none'")
    for field in ("wechat_webhook", "feishu_webhook"):
        webhook = data.get(field)
        if webhook:
            if not isinstance(webhook, str):
                raise ValueError(f"{field} must be text")
            from ...utils.url_validator import URLValidationError, validate_url_safe
            try:
                validate_url_safe(webhook)
            except URLValidationError as exc:
                raise ValueError(f"{field} is not allowed: {exc}") from exc
    notification_config = data.get("notification_config")
    if notification_config is not None:
        try:
            notification_config = NotificationConfig.model_validate(
                notification_config
            ).model_dump(exclude_none=True)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"invalid notification_config: {exc}") from exc

    normalized = {
        **data,
        "filename": display_name,
        "processing_options": normalized_options,
    }
    if "notify" in data:
        normalized["notify"] = notify
    if "notification_config" in data:
        normalized["notification_config"] = notification_config
    return normalized


def _upload_limits(config: dict) -> dict:
    configured = config.get("storage", {}).get("upload_limits")
    fields = ("max_file_mib", "max_media_hours", "receive_concurrency", "upload_temp_budget_mib")
    if not isinstance(configured, dict):
        return {field: None for field in fields}
    limits = {field: configured.get(field) for field in fields}
    for field in ("max_file_mib", "max_media_hours", "upload_temp_budget_mib"):
        value = limits[field]
        if (
            not isinstance(value, (int, float)) or isinstance(value, bool)
            or not math.isfinite(value) or value <= 0
        ):
            return {field: None for field in fields}
    concurrency = limits["receive_concurrency"]
    if not isinstance(concurrency, int) or isinstance(concurrency, bool) or concurrency <= 0:
        return {field: None for field in fields}
    return limits


def _worker_ready(request: Request) -> bool:
    runtime = getattr(request.app.state, "runtime", None)
    processor = getattr(request.app.state, "queue_processor", None)
    return bool(
        runtime is not None
        and getattr(runtime, "started", False)
        and processor is not None
        and not processor.done()
        and getattr(processor.get_coro(), "__qualname__", "") == "process_task_queue"
        and hasattr(runtime, "reserve_upload_temp")
    )


def _uploads_enabled(request: Request, config: dict, limits: dict) -> bool:
    return (
        os.environ.get("VTA_UPLOADS_ENABLED") == "true"
        and all(value is not None for value in limits.values())
        and _worker_ready(request)
    )


def _reject(reason: str, error: ApiError) -> ApiError:
    logger.warning("UPLOAD_INTAKE_REJECTED reason={} status={}", reason, error.status_code)
    return error


def _receipt(upload: dict) -> dict:
    accepted = upload.get("state") == "accepted" and not upload.get("error_code")
    state = upload.get("state") if not upload.get("error_code") else "failed"
    return {
        "upload_id": upload["upload_id"],
        "state": state,
        "task_id": upload.get("root_task_id") if accepted else None,
        "view_token": upload.get("view_token") if accepted else None,
        "retention": upload["retention"],
        "expires_at": upload.get("expires_at"),
        "share_active": bool(
            accepted
            and not upload.get("error_code")
            and get_cache_manager().local_upload_share_is_active(upload)
        ),
        "error_code": upload.get("error_code"),
    }


@router.get("/capabilities")
async def upload_capabilities(
    request: Request,
    user_info: dict = Depends(verify_token),
):
    config = get_config()
    limits = _upload_limits(config)
    return {
        "enabled": _uploads_enabled(request, config, limits),
        "default_retention": "30d",
        "retention_options": ["30d", "never"],
        "limits": limits,
    }


@router.post(
    "",
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "application/octet-stream": {
                    "schema": {"type": "string", "format": "binary"}
                }
            },
        }
    },
)
async def receive_upload(
    request: Request,
    idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
    metadata_header: str | None = Header(None, alias="X-Upload-Metadata"),
    user_info: dict = Depends(verify_token),
):
    """Receive one durable upload and publish its already-committed receipt.

    SQL acceptance and queue publication are the受理 boundary. Notification is
    deliberately outside that transaction: an unexpected router/formatting/send
    exception remains visible as HTTP 500 after the accepted row and queue item
    exist, while an explicit channel ``False`` keeps the normal HTTP 202 and
    emits the existing grepable failure log. There is no rollback or replay
    path tied to notification delivery.
    """
    # No body parameter: FastAPI resolves auth and these bounded headers before streaming.
    config = get_config()
    limits = _upload_limits(config)
    if not _uploads_enabled(request, config, limits):
        raise _reject("disabled_or_not_ready", ApiError(503, "upload_disabled_or_not_ready"))
    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if content_type != "application/octet-stream":
        raise _reject("content_type", ApiError(415, "upload_content_type"))
    if not idempotency_key:
        raise _reject("idempotency_key", ApiError(400, "upload_idempotency_key"))
    try:
        metadata = decode_upload_metadata(metadata_header or "")
    except ValueError as exc:
        raise _reject("metadata", ApiError(400, "upload_metadata", detail=str(exc))) from exc
    max_bytes = int(limits["max_file_mib"] * _MIB)
    if metadata["byte_size"] > max_bytes:
        raise _reject("declared_size", ApiError(413, "upload_declared_size"))

    cache_manager = get_cache_manager()
    try:
        intent = cache_manager.register_local_upload(
            owner_user_id=user_info.get("user_id"),
            idempotency_key=idempotency_key,
            retention=metadata["retention"],
            intent_metadata=metadata,
        )
    except ValueError as exc:
        message = str(exc)
        status = 409 if "metadata conflicts" in message else 410 if "24-hour" in message else 400
        raise _reject("idempotency", ApiError(status, "upload_idempotency", detail=message)) from exc
    if not intent["_created"]:
        return JSONResponse(_receipt(cache_manager.get_local_upload_by_id(intent["upload_id"])), status_code=200)

    upload_id = intent["upload_id"]
    task_id = cache_manager.generate_task_id()
    runtime = request.app.state.runtime
    receiver_acquired = runtime.upload_receiver_slots.acquire(blocking=False)
    inflight = get_inflight_registry()
    inflight_acquired = False
    budget_acquired = False
    body_started = False
    transferred = False
    failure_code = None
    temp_manager = get_temp_manager()
    task_dir = None
    media_path = None
    if not receiver_acquired:
        failure_code = "receiver_limit"
        cache_manager.set_local_upload_error(upload_id, failure_code)
        raise _reject(failure_code, ApiError(503, "upload_receiver_limit"))
    try:
        if not inflight.try_register("transcription", task_id):
            failure_code = "inflight_limit"
            cache_manager.set_local_upload_error(upload_id, failure_code)
            raise _reject(failure_code, ApiError(503, "upload_inflight_limit"))
        inflight_acquired = True
        declared_bytes = metadata["byte_size"]
        # Reserve only the durable source before receiving it. ASR output and
        # atomic-writer peaks are admitted at their real write boundary by the
        # worker; a fixed multiple would reject valid small-output jobs.
        reservation_bytes = declared_bytes
        budget_bytes = int(limits["upload_temp_budget_mib"] * _MIB)
        if not runtime.reserve_upload_temp(
            task_id, reservation_bytes, budget_bytes, str(temp_manager.base_dir)
        ):
            failure_code = "upload_temp_budget"
            cache_manager.set_local_upload_error(upload_id, failure_code)
            raise _reject(failure_code, ApiError(507, "upload_temp_budget"))
        budget_acquired = True
        task_dir = temp_manager.create_task_dir(task_id)
        temp_manager.mark_active(task_id)
        media_path = task_dir / "upload-source.bin"
        cache_manager.set_local_upload_receiving_path(upload_id, str(media_path))
        task_queue = get_task_queue()
        if task_queue.full():
            failure_code = "queue_full"
            cache_manager.set_local_upload_error(upload_id, failure_code)
            raise _reject(failure_code, ApiError(503, "upload_queue_full"))
        body_started = True
        observed_bytes = 0
        digest = hashlib.sha256()
        with os.fdopen(
            os.open(media_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb"
        ) as media_file:
            async for chunk in request.stream():
                if not chunk:
                    continue
                next_size = observed_bytes + len(chunk)
                if next_size > max_bytes or next_size > declared_bytes:
                    failure_code = "observed_size_limit"
                    raise _reject(failure_code, ApiError(413, "upload_observed_size_limit"))
                digest.update(chunk)
                media_file.write(chunk)
                observed_bytes = next_size
            if observed_bytes != declared_bytes:
                failure_code = "declared_size_mismatch"
                raise _reject(failure_code, ApiError(400, "upload_declared_size_mismatch"))
            media_file.flush()
            os.fsync(media_file.fileno())

        if not runtime.reserve_upload_temp(
            task_id, observed_bytes, budget_bytes, str(temp_manager.base_dir)
        ):
            failure_code = "upload_temp_budget"
            cache_manager.set_local_upload_error(upload_id, failure_code)
            raise _reject(failure_code, ApiError(507, "upload_temp_budget"))

        if task_queue.full():
            failure_code = "queue_full"
            cache_manager.set_local_upload_error(upload_id, failure_code)
            raise _reject(failure_code, ApiError(503, "upload_queue_full"))
        media_id = f"upload_{uuid.uuid4().hex}"
        notification_webhooks = resolve_notify_targets(user_info, metadata)
        try:
            accepted = cache_manager.accept_local_upload(
                upload_id,
                media_id=media_id,
                title=metadata["title"] or metadata["filename"],
                processing_options=metadata["processing_options"],
                task_id=task_id,
                filename=metadata["filename"],
                source_url=metadata["source_url"],
                request_metadata=metadata,
                media_path=str(media_path),
                byte_size=observed_bytes,
                sha256=digest.hexdigest(),
                notification_webhooks=notification_webhooks,
                notify_targets=notification_webhooks,
                enqueue=task_queue.put_nowait,
            )
        except ValueError as exc:
            if "24-hour" in str(exc):
                failure_code = "acceptance_window_expired"
                raise _reject(failure_code, ApiError(410, "upload_acceptance_window_expired")) from exc
            raise
        transferred = True
        from ...utils.notifications.task_formatter import local_upload_notification_fields

        accepted_record = cache_manager.get_local_upload_by_id(accepted["upload_id"])
        notification_fields = local_upload_notification_fields(accepted_record)
        notification_result = get_notification_router().send_view_link(
            title=notification_fields["title"],
            view_token=accepted_record["view_token"],
            original_url=notification_fields["source_url"],
            source_label=notification_fields["source_label"],
            task_id=task_id,
            webhooks=notification_webhooks,
        )
        if not isinstance(notification_result, dict) or not any(
            result is True for result in notification_result.values()
        ):
            logger.error(
                "UPLOAD_ACCEPTED_NOTIFICATION_FAILED task_id={} result={}",
                task_id,
                notification_result,
            )
        return JSONResponse(_receipt(accepted_record), status_code=202)
    except asyncio.QueueFull as exc:
        failure_code = "queue_full"
        raise _reject(failure_code, ApiError(503, "upload_queue_full")) from exc
    finally:
        # Ownership: a failure in one cleanup step must not short-circuit the
        # remaining releases. The receiver slot is released by the outermost
        # finally; inflight goes before directory cleanup; the temp budget is
        # intentionally kept while the untransferred directory still occupies
        # disk (no false reclamation). Errors still propagate as HTTP 500.
        try:
            try:
                if body_started and not transferred:
                    failure_code = failure_code or "receive_incomplete"
                    cache_manager.set_local_upload_error(upload_id, failure_code)
                    logger.warning(
                        "UPLOAD_INTAKE_FAILED reason={} status=failed", failure_code
                    )
            finally:
                if not transferred:
                    if inflight_acquired:
                        inflight.release("transcription", task_id)
                    if task_dir is not None:
                        temp_manager.clean_up_task(task_id)
                        if task_dir.exists():
                            raise OSError(
                                f"failed to remove untransferred upload directory: {task_dir}"
                            )
                    if budget_acquired:
                        runtime.release_upload_temp(task_id)
        finally:
            if receiver_acquired:
                runtime.upload_receiver_slots.release()


@router.get("/by-idempotency-key/{key}")
async def get_upload_receipt(
    key: str,
    user_info: dict = Depends(verify_token),
):
    upload = get_cache_manager().get_local_upload_by_owner_key(user_info.get("user_id"), key)
    if upload is None:
        raise ApiError(404, "upload_receipt_not_found")
    return _receipt(upload)


@router.delete("/{upload_id}/share")
async def stop_upload_share(
    upload_id: str,
    user_info: dict = Depends(verify_token),
):
    cache_manager = get_cache_manager()
    upload = cache_manager.get_local_upload_by_id(upload_id)
    if upload is None or upload["owner_user_id"] != user_info.get("user_id"):
        raise ApiError(404, "upload_record_not_found")
    if upload["state"] != "accepted":
        raise ApiError(409, "upload_not_accepted")
    revoked = cache_manager.revoke_local_upload(upload_id)
    return {
        "upload_id": upload_id,
        "share_active": False,
        "revoked_at": revoked["revoked_at"],
    }
