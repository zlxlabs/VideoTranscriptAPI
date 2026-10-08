"""Bounded raw-byte local upload API."""

import base64
import binascii
import json
import re
from pathlib import PurePosixPath
from urllib.parse import urlsplit

from ..processing_options import normalize_processing_options

_METADATA_MAX_BYTES = 4096
_BASE64URL = re.compile(r"^[A-Za-z0-9_-]+={0,2}$")
_ALLOWED_METADATA = {
    "filename", "byte_size", "title", "source_url", "retention", "processing_options"
}


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
    if any(ord(char) < 32 or ord(char) == 127 for char in filename):
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
        or any(ord(char) < 32 and char not in "\t\n\r" for char in title)
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
            or any(ord(char) < 32 or ord(char) == 127 for char in source_url)
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
