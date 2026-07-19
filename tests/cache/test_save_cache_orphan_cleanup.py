"""Regression test: save_cache() must clean up known stale artifact files
before writing into a directory that has no live video_cache row pointing
at it, so a brand new row never ends up serving mixed old/new content.

Background (Z3, PR3 review hardening 最后一批): Y5 (see
test_cleanup_delete_ordering.py) reordered cleanup_old_cache()'s per-record
deletion so the DB row is deleted strictly before shutil.rmtree() runs --
if rmtree then fails, the leftover directory is treated as a harmless
orphan, because no video_cache row references it any more.

That "harmless" framing misses one thing: the directory's path is
deterministic (cache_dir/platform/YYYY/YYYYMM/media_id, computed purely
from platform + media_id -- see CacheManager._get_file_path), so the very
next request for the same (platform, media_id) recomputes and reuses the
exact same path. Before this fix, save_cache() only ever wrote this
round's transcript file (transcript_funasr.json OR transcript_
capswriter.txt/.json, depending on transcript_type) into that directory --
any stale artifact left over from the orphaned directory (old-format
transcript, old llm_summary.txt, ...) stayed right where it was. get_cache()
reads transcript_funasr.json ahead of transcript_capswriter.txt
unconditionally (fixed priority, not "whichever is newest"), so a stale
funasr leftover would silently outrank a freshly-written capswriter
transcript under the brand new row -- the new row would resolve to a mix
of fresh DB metadata and stale file content.

The fix: save_cache(), still inside the same per-media media_lock critical
section it already holds (U1/Y6), checks whether the (platform, media_id)
currently has ANY live video_cache row (either use_speaker_recognition
variant) before writing. No live row at all is the only situation Y5's own
DB-first-then-rmtree ordering can produce a directory-without-a-row in --
so it's the only situation in which files in that directory are safe to
delete. If a live row exists (either the same variant being normally
re-saved, or the W3 sibling-variant-sharing-the-same-directory case), the
files there are still in active use and cleanup is skipped entirely.

Console output: English only, no emoji (project convention).
"""
import uuid
from pathlib import Path

import pytest

from src.video_transcript_api.cache.cache_manager import CacheManager


@pytest.fixture
def cm(tmp_path):
    manager = CacheManager(cache_dir=str(tmp_path / "cache"))
    yield manager
    manager.close()


def _cache_row_exists(cm, platform: str, media_id: str) -> bool:
    with cm._get_cursor() as cursor:
        cursor.execute(
            "SELECT 1 FROM video_cache WHERE platform = ? AND media_id = ?",
            (platform, media_id),
        )
        return cursor.fetchone() is not None


def _make_orphaned_directory(cm, platform, media_id, files):
    """Create files directly at the exact path save_cache() would compute
    for (platform, media_id) -- i.e. CacheManager._get_file_path()'s own
    deterministic result -- with no corresponding video_cache row. This
    reproduces the precondition Y5 leaves behind after a failed rmtree
    (directory survives, DB row is already gone) directly, instead of
    driving it through cleanup_old_cache()'s own date-cutoff machinery
    (whose sibling test module, test_cleanup_delete_ordering.py, already
    locks down that *cleanup* itself orders DB-delete-before-rmtree
    correctly; this file is only concerned with what save_cache() does
    when it later lands on a directory in that state).
    """
    file_path = cm._get_file_path(platform, media_id)
    file_path.mkdir(parents=True, exist_ok=True)
    for filename, content in files.items():
        (file_path / filename).write_text(content, encoding="utf-8")
    return file_path


class TestSaveCacheCleansOrphanedResidueOnFreshRow:
    def test_stale_funasr_and_llm_leftovers_no_longer_shadow_fresh_capswriter_save(
        self, cm,
    ):
        platform, media_id = "youtube", f"vid-{uuid.uuid4().hex[:8]}"
        orphan_path = _make_orphaned_directory(cm, platform, media_id, {
            "transcript_funasr.json": '{"stale": true}',
            "llm_summary.txt": "STALE SUMMARY",
        })
        assert not _cache_row_exists(cm, platform, media_id), (
            "test setup must reproduce Y5's orphan precondition: directory "
            "exists, no live row references it"
        )

        result = cm.save_cache(
            platform=platform,
            url=f"https://example.com/{media_id}",
            media_id=media_id,
            use_speaker_recognition=False,
            transcript_data="fresh capswriter transcript",
            transcript_type="capswriter",
            title="Fresh title",
            author="Fresh author",
        )
        assert result is not None

        fresh_file_path = cm.cache_dir / Path(result["files_loc"])
        assert fresh_file_path == orphan_path, (
            "sanity check: the deterministic path must actually collide "
            "with the orphaned directory for this test to be meaningful"
        )
        assert not (fresh_file_path / "transcript_funasr.json").exists(), (
            "stale funasr leftover must be cleaned up before a brand new "
            "row is written (red: old code leaves it in place, shadowing "
            "the fresh capswriter save)"
        )
        assert not (fresh_file_path / "llm_summary.txt").exists(), (
            "stale LLM artifact must be cleaned up alongside the stale "
            "transcript (red: old code leaves it in place)"
        )
        assert (fresh_file_path / "transcript_capswriter.txt").exists()

        cache_data = cm.get_cache(
            platform=platform, media_id=media_id, use_speaker_recognition=False,
        )
        assert cache_data is not None
        assert cache_data["transcript_type"] == "capswriter", (
            "get_cache must resolve to the freshly written format, not be "
            "shadowed by a stale transcript_funasr.json it checks first "
            "(red: old code still has content_type == 'funasr' here)"
        )
        assert cache_data["transcript_data"] == "fresh capswriter transcript"
        assert "llm_summary" not in cache_data, (
            "the stale llm_summary.txt must not leak into a brand new "
            "row's cache data"
        )

    def test_orphan_cleanup_only_removes_known_artifact_filenames(self, cm):
        """The cleanup must be a targeted unlink of the known artifact
        filename list, not a directory-wide sweep -- an unrelated file
        sitting in the same directory (e.g. something a future artifact
        type would add, not yet in the known list) must survive."""
        platform, media_id = "youtube", f"vid-{uuid.uuid4().hex[:8]}"
        orphan_path = _make_orphaned_directory(cm, platform, media_id, {
            "transcript_funasr.json": '{"stale": true}',
            "unrelated_future_artifact.bin": "not a known artifact filename",
        })

        result = cm.save_cache(
            platform=platform,
            url=f"https://example.com/{media_id}",
            media_id=media_id,
            use_speaker_recognition=False,
            transcript_data="fresh capswriter transcript",
            transcript_type="capswriter",
        )
        assert result is not None

        assert (orphan_path / "unrelated_future_artifact.bin").exists(), (
            "cleanup must be scoped to the known artifact filename list, "
            "not a wholesale directory wipe"
        )
        assert not (orphan_path / "transcript_funasr.json").exists()


class TestSaveCacheSkipsCleanupWhenAnyRowIsStillLive:
    def test_live_sibling_variant_directory_is_not_touched(self, cm):
        """Non-regression (W3 sharing): _get_file_path() does not depend on
        use_speaker_recognition, so both variants of the same (platform,
        media_id) share one directory. If the OTHER variant already has a
        live row there, a brand new row for the current variant is NOT an
        orphan-residue scenario -- it's normal sharing -- and must not
        trigger cleanup, or it would destroy the sibling's still-needed
        files."""
        platform, media_id = "youtube", f"vid-{uuid.uuid4().hex[:8]}"
        sibling = cm.save_cache(
            platform=platform,
            url=f"https://example.com/{media_id}",
            media_id=media_id,
            use_speaker_recognition=True,
            transcript_data={"ok": True},
            transcript_type="funasr",
            title="Sibling title",
            author="Sibling author",
        )
        assert sibling is not None
        sibling_file_path = cm.cache_dir / Path(sibling["files_loc"])
        assert (sibling_file_path / "transcript_funasr.json").exists()

        # Fresh row for the OTHER variant of the same media -- no row for
        # THIS exact (platform, media_id, use_speaker_recognition) tuple
        # yet, but the sibling variant is alive and shares the directory.
        result = cm.save_cache(
            platform=platform,
            url=f"https://example.com/{media_id}",
            media_id=media_id,
            use_speaker_recognition=False,
            transcript_data="capswriter body",
            transcript_type="capswriter",
            title="Other title",
            author="Other author",
        )
        assert result is not None
        assert cm.cache_dir / Path(result["files_loc"]) == sibling_file_path

        assert (sibling_file_path / "transcript_funasr.json").exists(), (
            "a live sibling variant's files must survive when a fresh row "
            "is created for the other variant sharing the same directory"
        )

    def test_resaving_the_same_variant_is_not_treated_as_orphan_cleanup(self, cm):
        """Non-regression: re-saving the SAME (platform, media_id,
        use_speaker_recognition) tuple (e.g. recalibrate producing a new
        transcript for an already-cached task) is an ordinary update via
        INSERT OR REPLACE, not a fresh-row-into-an-orphan scenario -- the
        existing row already live for this exact tuple must suppress
        cleanup."""
        platform, media_id = "youtube", f"vid-{uuid.uuid4().hex[:8]}"
        first = cm.save_cache(
            platform=platform,
            url=f"https://example.com/{media_id}",
            media_id=media_id,
            use_speaker_recognition=False,
            transcript_data="first body",
            transcript_type="capswriter",
        )
        assert first is not None
        file_path = cm.cache_dir / Path(first["files_loc"])
        (file_path / "llm_summary.txt").write_text("real summary", encoding="utf-8")

        second = cm.save_cache(
            platform=platform,
            url=f"https://example.com/{media_id}",
            media_id=media_id,
            use_speaker_recognition=False,
            transcript_data="second body",
            transcript_type="capswriter",
        )
        assert second is not None
        assert (file_path / "llm_summary.txt").exists(), (
            "an ordinary re-save of the same variant must not wipe "
            "co-located LLM artifacts as if it were orphan cleanup"
        )

        cache_data = cm.get_cache(
            platform=platform, media_id=media_id, use_speaker_recognition=False,
        )
        assert cache_data["transcript_data"] == "second body"


class TestSaveCacheAbortsWhenOrphanCleanupConflicts:
    """K2 (CI review round 3, major): _cleanup_orphaned_artifact_files
    deleting a stale artifact can itself fail (e.g. a transient permission
    or filesystem error). Before this fix, save_cache() logged the failure
    and pressed on regardless -- committing a brand new video_cache row
    even though the leftover stale file would keep shadowing (or otherwise
    conflicting with) this round's freshly written content under
    get_cache()'s fixed read priority. The fix: a delete failure on a
    filename this round does NOT overwrite is a real conflict, and
    save_cache() must abort (return None, no new row) instead of reporting
    success."""

    def test_transcript_funasr_delete_failure_conflicts_with_capswriter_save(
        self, cm, monkeypatch,
    ):
        """The reader-prioritized transcript_funasr.json fails to delete,
        and this round is writing a CapsWriter transcript (which never
        touches transcript_funasr.json) -- the leftover funasr file would
        keep shadowing the fresh capswriter content under get_cache()'s
        fixed read priority (funasr checked before capswriter). This is
        exactly the scenario named in the CI acceptance criteria."""
        platform, media_id = "youtube", f"vid-{uuid.uuid4().hex[:8]}"
        orphan_path = _make_orphaned_directory(cm, platform, media_id, {
            "transcript_funasr.json": '{"stale": true}',
        })

        original_unlink = Path.unlink

        def _boom_unlink(self, *args, **kwargs):
            if self.name == "transcript_funasr.json":
                raise OSError("permission denied (simulated)")
            return original_unlink(self, *args, **kwargs)

        monkeypatch.setattr(Path, "unlink", _boom_unlink)

        result = cm.save_cache(
            platform=platform,
            url=f"https://example.com/{media_id}",
            media_id=media_id,
            use_speaker_recognition=False,
            transcript_data="fresh capswriter transcript",
            transcript_type="capswriter",
            title="Fresh title",
            author="Fresh author",
        )

        assert result is None, (
            "save_cache must fail loudly instead of committing a new row "
            "that would resolve to the stale, undeletable "
            "transcript_funasr.json"
        )
        assert not _cache_row_exists(cm, platform, media_id), (
            "no new video_cache row should be committed when the orphan "
            "residue conflict is unresolved"
        )
        assert (orphan_path / "transcript_funasr.json").exists(), (
            "sanity check on the injected failure: the stale file that "
            "failed to delete is still there"
        )

        cache_data = cm.get_cache(
            platform=platform, media_id=media_id, use_speaker_recognition=False,
        )
        assert cache_data is None, (
            "get_cache must not return the stale funasr content under a "
            "row that was never actually committed"
        )

    def test_delete_failure_on_a_filename_this_round_overwrites_is_not_a_conflict(
        self, cm, monkeypatch,
    ):
        """Companion/non-regression: when this round is ALSO writing
        transcript_funasr.json (same format), a delete failure on that
        exact file does not matter -- the subsequent atomic write replaces
        its content regardless, so it must NOT be treated as a conflict."""
        platform, media_id = "youtube", f"vid-{uuid.uuid4().hex[:8]}"
        _make_orphaned_directory(cm, platform, media_id, {
            "transcript_funasr.json": '{"stale": true}',
        })

        original_unlink = Path.unlink

        def _boom_unlink(self, *args, **kwargs):
            if self.name == "transcript_funasr.json":
                raise OSError("permission denied (simulated)")
            return original_unlink(self, *args, **kwargs)

        monkeypatch.setattr(Path, "unlink", _boom_unlink)

        result = cm.save_cache(
            platform=platform,
            url=f"https://example.com/{media_id}",
            media_id=media_id,
            use_speaker_recognition=False,
            transcript_data={"fresh": True},
            transcript_type="funasr",
            title="Fresh title",
            author="Fresh author",
        )

        assert result is not None, (
            "a delete failure on a file this round overwrites via its own "
            "atomic write must not be treated as a conflict"
        )

        cache_data = cm.get_cache(
            platform=platform, media_id=media_id, use_speaker_recognition=False,
        )
        assert cache_data is not None
        assert cache_data["transcript_data"] == {"fresh": True}

    def test_delete_failure_on_unrelated_llm_artifact_still_conflicts(
        self, cm, monkeypatch,
    ):
        """Not just the transcript file -- LLM/speaker artifacts residue
        get read unconditionally by get_cache() whenever present, so a
        delete failure on any of them is a conflict too (this round never
        overwrites them; only save_llm_result/save_llm_status/
        save_speaker_mapping do)."""
        platform, media_id = "youtube", f"vid-{uuid.uuid4().hex[:8]}"
        _make_orphaned_directory(cm, platform, media_id, {
            "llm_summary.txt": "STALE SUMMARY",
        })

        original_unlink = Path.unlink

        def _boom_unlink(self, *args, **kwargs):
            if self.name == "llm_summary.txt":
                raise OSError("permission denied (simulated)")
            return original_unlink(self, *args, **kwargs)

        monkeypatch.setattr(Path, "unlink", _boom_unlink)

        result = cm.save_cache(
            platform=platform,
            url=f"https://example.com/{media_id}",
            media_id=media_id,
            use_speaker_recognition=False,
            transcript_data="fresh capswriter transcript",
            transcript_type="capswriter",
        )

        assert result is None
        assert not _cache_row_exists(cm, platform, media_id)
