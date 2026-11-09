"""Regression tests for #266: space-polling test doubles must stay scoped.

`make_space_downloader` (tests/unit/test_media_resolver_downloader.py) used to
patch time.monotonic/time.sleep on the shared stdlib `time` module and
requests.get on the shared `requests` module. Those attributes are process
globals: while installed, a time.sleep() from ANY other code in the same pytest
process was routed into the helper's recording double, so sleep_log collected
foreign waits, sum(sleeps) was inflated, and
TestSpaceStatusMapping.test_503_retried_then_fails_visibly_when_budget_exhausted
intermittently failed its `assert sum(sleeps) <= 1800` in the full suite.

The fix binds the doubles to names owned by the module under test
(video_transcript_api.downloaders.media_resolver): monkeypatch.setattr replaces
the `time` and `requests` attributes on that module object only, leaving the
shared stdlib modules untouched. Both tests below fail against the pre-fix
helper and pass against the module-scoped one.
"""

import time

import requests

from tests.unit.test_media_resolver_downloader import (
    _SPACE_ID,
    _SPACE_STREAM_URL,
    load_space_fixture,
    make_space_downloader,
    space_200_response,
    space_202_response,
)


def test_foreign_sleep_stays_out_of_sleep_log(monkeypatch):
    """A time.sleep() issued elsewhere in the process (simulated right before
    polling) must not enter sleep_log: sleep_log holds only the waits made by
    the code under test. Against the pre-fix helper the injected call went
    through the shared-module double and landed in sleep_log, so the exact
    list assertion went red (recorded [0.0, 5.0] instead of [5.0])."""
    dl, _calls, sleeps = make_space_downloader(
        monkeypatch, [space_202_response(), space_200_response()]
    )
    time.sleep(0)  # foreign sleep, as any other test in the process would make
    out = dl.download_file(_SPACE_STREAM_URL, f"twitter_space_{_SPACE_ID}.m4a")
    assert out is not None
    retry_after = float(load_space_fixture("stream_202.json")["retry_after"])
    assert sleeps == [retry_after]


def test_shared_time_and_requests_modules_stay_unpatched(monkeypatch):
    """Installing the doubles must not swap attributes on the shared `time` or
    `requests` modules; those are process globals. Against the pre-fix helper
    both were patched in place, so every identity assertion below went red."""
    real_monotonic = time.monotonic
    real_sleep = time.sleep
    real_get = requests.get
    make_space_downloader(monkeypatch, [space_200_response()], with_clock=True)
    assert time.monotonic is real_monotonic
    assert time.sleep is real_sleep
    assert requests.get is real_get
