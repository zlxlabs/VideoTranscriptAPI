import itertools

import pytest

from video_transcript_api.api.routes.uploads import _receipt
from video_transcript_api.cache.cache_manager import CacheManager


@pytest.mark.parametrize(
    "root_status,expires_at,error_code",
    itertools.product(
        (None, "processing", "success", "failed"),
        (None, "2099-01-01 00:00:00", "2000-01-01 00:00:00"),
        (None, "upload_failed"),
    ),
)
def test_receipt_share_active_matches_authority_and_error_condition(
    root_status, expires_at, error_code
):
    upload = {
        "upload_id": "upload-test",
        "state": "accepted",
        "retention": "30d",
        "expires_at": expires_at,
        "revoked_at": None,
        "root_status": root_status,
        "error_code": error_code,
    }

    expected = CacheManager.local_upload_share_is_active(upload) and not error_code
    receipt = _receipt(upload)

    assert receipt["share_active"] is expected
    assert receipt["state"] == ("failed" if error_code else "accepted")
