"""Post-hoc title/author editing for shared transcript pages (vta-261031).

All SQLite access goes through CacheManager._get_cursor() so the writes stay
on the same connection/transaction machinery as every other writer: the two
UPDATE statements (video_cache + task_status) run inside one cursor context,
which commits on clean exit and rolls back on error.

Write granularity is the whole (platform, media_id) media, not a single
video_cache row: get_cache(use_speaker_recognition=False) orders by
use_speaker_recognition DESC, so a speaker=0 view token may display the
speaker=1 row -- editing only the display row would silently not take
effect on the page.

After a successful edit every og_card.*.png under the media's cache
directory is removed: routes/views.py::_render_og_share_card treats a PNG as
fresh only while its mtime is >= llm_share_card.json's mtime, so deleting
the PNG forces a redraw with the new DB author on the next /og request.
"""

from pathlib import Path

from ..context import ApiError
from ...utils.logging import setup_logger

logger = setup_logger("metadata_editor")

_IN_FLIGHT_STATUSES = ("queued", "processing", "calibrating")


def check_media_constraints(
    cache_manager,
    audit_logger,
    *,
    platform: str,
    media_id: str,
    user_id: str,
    is_legacy: bool,
) -> None:
    """Raise when the media must not be edited by this caller.

    Enumerates every task_status row of the (platform, media_id) media,
    skipping content-expired rows (audit snapshot content_expired=1, same
    filter semantics as get_task_by_view_token / list_tasks_by_view_token):

    - any non-expired row whose submitted_by differs from the caller
      (legacy single-token users are exempt) -> metadata_shared_denied:
      a media shared with another account must never be partially rewritten;
    - any non-expired row still queued/processing/calibrating ->
      metadata_in_flight: a terminal write-back could clobber the new
      title/author with stale values.
    """
    with cache_manager._get_cursor() as cursor:
        cursor.execute(
            "SELECT task_id, status, submitted_by FROM task_status "
            "WHERE platform = ? AND media_id = ?",
            (platform, media_id),
        )
        rows = [dict(row) for row in cursor.fetchall()]

    shared_by_others = False
    in_flight = False
    for row in rows:
        if audit_logger is not None:
            snapshot = audit_logger.get_task_snapshot(row["task_id"])
            if snapshot and snapshot.get("content_expired"):
                continue
        if not is_legacy and row["submitted_by"] != user_id:
            shared_by_others = True
        if row["status"] in _IN_FLIGHT_STATUSES:
            in_flight = True

    if shared_by_others:
        logger.warning(
            "edit_metadata rejected: media {}/{} has task rows submitted by "
            "other accounts (caller={})",
            platform,
            media_id,
            user_id,
        )
        raise ApiError(403, "metadata_shared_denied")
    if in_flight:
        logger.warning(
            "edit_metadata rejected: media {}/{} still has in-flight tasks",
            platform,
            media_id,
        )
        raise ApiError(409, "metadata_in_flight")


def apply_metadata_update(
    cache_manager,
    *,
    platform: str,
    media_id: str,
    title: "str | None",
    author: "str | None",
) -> "dict[str, str | None]":
    """Rewrite title/author for the whole media; return the previous values.

    Single transaction: both UPDATEs share one _get_cursor() context, so a
    failure rolls back video_cache and task_status together. Returns the
    old display values (read with the same ordering get_cache uses, so the
    caller's audit line shows old->new for what the page used to serve).
    """
    with cache_manager._get_cursor() as cursor:
        cursor.execute(
            "SELECT title, author FROM video_cache "
            "WHERE platform = ? AND media_id = ? "
            "ORDER BY use_speaker_recognition DESC LIMIT 1",
            (platform, media_id),
        )
        previous = cursor.fetchone()
        old_title = previous["title"] if previous else None
        old_author = previous["author"] if previous else None

        set_clauses = []
        params = []
        if title is not None:
            set_clauses.append("title = ?")
            params.append(title)
        if author is not None:
            set_clauses.append("author = ?")
            params.append(author)
        assignments = ", ".join(set_clauses)

        cursor.execute(
            f"UPDATE video_cache SET {assignments} "
            "WHERE platform = ? AND media_id = ?",
            (*params, platform, media_id),
        )
        cursor.execute(
            f"UPDATE task_status SET {assignments} "
            "WHERE platform = ? AND media_id = ?",
            (*params, platform, media_id),
        )

    _invalidate_og_pngs(cache_manager, platform=platform, media_id=media_id)
    return {"old_title": old_title, "old_author": old_author}


def _invalidate_og_pngs(cache_manager, *, platform: str, media_id: str) -> None:
    """Delete every og_card.*.png under the media's cache directory.

    Missing PNGs are a no-op (first render has nothing to invalidate); the
    next /og request re-renders because the PNG no longer exists.
    """
    with cache_manager._get_cursor() as cursor:
        cursor.execute(
            "SELECT files_loc FROM video_cache "
            "WHERE platform = ? AND media_id = ?",
            (platform, media_id),
        )
        rows = [dict(row) for row in cursor.fetchall()]

    cache_dir = Path(cache_manager.cache_dir)
    for row in rows:
        media_dir = cache_dir / row["files_loc"]
        for png_path in sorted(media_dir.glob("og_card.*.png")):
            png_path.unlink()
            logger.info("invalidated OG share card: {}", png_path)
