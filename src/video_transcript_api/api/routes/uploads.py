"""Bounded raw-byte local upload API."""

import asyncio
import base64
import binascii
import datetime
import hashlib
import json
import math
import os
import re
import uuid
from pathlib import PurePosixPath
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from ..context import (
    get_cache_manager,
    get_config,
    get_inflight_registry,
    get_task_queue,
    get_temp_manager,
)
from ..processing_options import normalize_processing_options
from ..services.transcription import verify_token
from ...utils.logging import logger

router = APIRouter(prefix="/api/uploads", tags=["uploads"])
_METADATA_MAX_BYTES = 4096
_BASE64URL = re.compile(r"^[A-Za-z0-9_-]+={0,2}$")
_ALLOWED_METADATA = {
    "filename", "byte_size", "title", "source_url", "retention", "processing_options"
}
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
    if not isinstance(data, dict) or set(data) != _ALLOWED_METADATA:
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

    return {
        **data,
        "filename": display_name,
        "processing_options": normalized_options,
    }


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
            and upload.get("revoked_at") is None
            and (
                upload["retention"] == "never"
                and upload.get("expires_at") is None
                or upload["retention"] == "30d"
                and (
                    upload.get("expires_at") is None
                    and upload.get("root_status") not in {"success", "failed"}
                    or upload.get("expires_at") is not None
                    and upload["expires_at"] > datetime.datetime.now(
                        datetime.timezone.utc
                    ).strftime("%Y-%m-%d %H:%M:%S")
                )
            )
        ),
        "error_code": upload.get("error_code"),
    }


def _reject(status_code: int, reason: str, detail: str) -> HTTPException:
    logger.warning("UPLOAD_INTAKE_REJECTED reason={} status={}", reason, status_code)
    return HTTPException(status_code=status_code, detail=detail)


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
    # No body parameter: FastAPI resolves auth and these bounded headers before streaming.
    config = get_config()
    limits = _upload_limits(config)
    if not _uploads_enabled(request, config, limits):
        raise _reject(503, "disabled_or_not_ready", "上传接收未启用或worker尚未就绪")
    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if content_type != "application/octet-stream":
        raise _reject(415, "content_type", "Content-Type必须为application/octet-stream")
    if not idempotency_key:
        raise _reject(400, "idempotency_key", "缺少Idempotency-Key")
    try:
        metadata = decode_upload_metadata(metadata_header or "")
    except ValueError as exc:
        raise _reject(400, "metadata", str(exc)) from exc
    max_bytes = int(limits["max_file_mib"] * _MIB)
    if metadata["byte_size"] > max_bytes:
        raise _reject(413, "declared_size", "上传文件超过允许大小")

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
        raise _reject(status, "idempotency", message) from exc
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
        raise _reject(503, failure_code, "上传接收并发已满")
    try:
        if not inflight.try_register("transcription", task_id):
            failure_code = "inflight_limit"
            cache_manager.set_local_upload_error(upload_id, failure_code)
            raise _reject(503, failure_code, "转录队列已满")
        inflight_acquired = True
        declared_bytes = metadata["byte_size"]
        # Keep one source-sized processing footprint reserved until the existing worker finishes.
        reservation_bytes = declared_bytes * 2
        budget_bytes = int(limits["upload_temp_budget_mib"] * _MIB)
        if not runtime.reserve_upload_temp(
            task_id, reservation_bytes, budget_bytes, str(temp_manager.base_dir)
        ):
            failure_code = "upload_temp_budget"
            cache_manager.set_local_upload_error(upload_id, failure_code)
            raise _reject(507, failure_code, "上传临时存储预算或可用磁盘空间不足")
        budget_acquired = True
        task_dir = temp_manager.create_task_dir(task_id)
        temp_manager.mark_active(task_id)
        media_path = task_dir / "upload-source.bin"
        cache_manager.set_local_upload_receiving_path(upload_id, str(media_path))
        task_queue = get_task_queue()
        if task_queue.full():
            failure_code = "queue_full"
            cache_manager.set_local_upload_error(upload_id, failure_code)
            raise _reject(503, failure_code, "转录队列已满，上传未正式受理")
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
                    raise _reject(413, failure_code, "实际上传字节超过声明或允许大小")
                digest.update(chunk)
                media_file.write(chunk)
                observed_bytes = next_size
            if observed_bytes != declared_bytes:
                failure_code = "declared_size_mismatch"
                raise _reject(400, failure_code, "实际上传字节与byte_size不一致")
            media_file.flush()
            os.fsync(media_file.fileno())

        if task_queue.full():
            failure_code = "queue_full"
            cache_manager.set_local_upload_error(upload_id, failure_code)
            raise _reject(503, failure_code, "转录队列已满，上传未正式受理")
        media_id = f"upload_{uuid.uuid4().hex}"
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
                enqueue=task_queue.put_nowait,
            )
        except ValueError as exc:
            if "24-hour" in str(exc):
                failure_code = "acceptance_window_expired"
                raise _reject(410, failure_code, "上传幂等key已超过24小时受理截止时间") from exc
            raise
        transferred = True
        return JSONResponse(_receipt(cache_manager.get_local_upload_by_id(accepted["upload_id"])), status_code=202)
    except asyncio.QueueFull as exc:
        failure_code = "queue_full"
        raise _reject(503, failure_code, "转录队列已满，上传未正式受理") from exc
    finally:
        if body_started and not transferred:
            failure_code = failure_code or "receive_incomplete"
            cache_manager.set_local_upload_error(upload_id, failure_code)
            logger.warning("UPLOAD_INTAKE_REJECTED reason={} status=failed", failure_code)
        if not transferred:
            if task_dir is not None:
                temp_manager.clean_up_task(task_id)
                if task_dir.exists():
                    raise OSError(f"failed to remove untransferred upload directory: {task_dir}")
            if budget_acquired:
                runtime.release_upload_temp(task_id)
            if inflight_acquired:
                inflight.release("transcription", task_id)
        if receiver_acquired:
            runtime.upload_receiver_slots.release()


@router.get("/by-idempotency-key/{key}")
async def get_upload_receipt(
    key: str,
    user_info: dict = Depends(verify_token),
):
    upload = get_cache_manager().get_local_upload_by_owner_key(user_info.get("user_id"), key)
    if upload is None:
        raise HTTPException(status_code=404, detail="上传回执不存在")
    return _receipt(upload)


@router.delete("/{upload_id}/share")
async def stop_upload_share(
    upload_id: str,
    user_info: dict = Depends(verify_token),
):
    cache_manager = get_cache_manager()
    upload = cache_manager.get_local_upload_by_id(upload_id)
    if upload is None or upload["owner_user_id"] != user_info.get("user_id"):
        raise HTTPException(status_code=404, detail="上传记录不存在")
    if upload["state"] != "accepted":
        raise HTTPException(status_code=409, detail="尚未正式受理的上传没有分享可关闭")
    revoked = cache_manager.revoke_local_upload(upload_id)
    return {
        "upload_id": upload_id,
        "share_active": False,
        "revoked_at": revoked["revoked_at"],
    }
