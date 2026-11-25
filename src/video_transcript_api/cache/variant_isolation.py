"""Exact cache-variant and generation selection for write paths."""
from pathlib import Path
from typing import Any, Optional


class CacheGenerationMismatch(RuntimeError):
    """A delayed writer no longer targets the published cache generation."""


def resolve_variant_artifact_dir(
    cache_manager: Any,
    platform: str,
    media_id: str,
    use_speaker_recognition: bool,
    expected_files_loc: Optional[str] = None,
) -> Optional[Path]:
    """Resolve only the exact variant and reject stale generation handoffs."""
    variant = int(bool(use_speaker_recognition))
    with cache_manager._get_cursor() as cursor:
        cursor.execute(
            "SELECT files_loc FROM video_cache "
            "WHERE platform = ? AND media_id = ? AND use_speaker_recognition = ?",
            (platform, media_id, variant),
        )
        row = cursor.fetchone()
    if row is None:
        return None
    files_loc = row["files_loc"]
    if expected_files_loc is not None and files_loc != expected_files_loc:
        raise CacheGenerationMismatch(
            f"CACHE-GENERATION-MISMATCH: {platform}/{media_id} "
            f"variant={variant} expected={expected_files_loc!r} current={files_loc!r}"
        )
    return cache_manager.cache_dir / Path(files_loc)
