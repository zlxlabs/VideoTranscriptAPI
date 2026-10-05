"""
Test Bilibili official API metadata fetching functionality
"""
import sys
import os

# Add project root to path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from src.video_transcript_api.downloaders.bilibili import BilibiliDownloader
from src.video_transcript_api.utils.logging import setup_logger

logger = setup_logger("test_bilibili_metadata")


def test_fetch_official_metadata():
    """Test fetching metadata from Bilibili official API"""
    print("\n" + "=" * 60)
    print("Testing Bilibili Official API Metadata Fetching")
    print("=" * 60)

    downloader = BilibiliDownloader()
    test_url = "https://www.bilibili.com/video/BV1zW2vB2Ey2"
    bvid = "BV1zW2vB2Ey2"

    # Test 1: Direct API call
    print("\n[Test 1] Direct API metadata fetch")
    print("-" * 60)
    metadata = downloader._fetch_bilibili_official_metadata(bvid)

    if metadata:
        print(f"Title: {metadata.get('title')}")
        print(f"Author: {metadata.get('author')}")
        print(f"Author ID: {metadata.get('author_id')}")
        print(f"Duration: {metadata.get('duration')} seconds")
        print(f"Description length: {len(metadata.get('description', ''))} characters")
        print(f"Description preview: {metadata.get('description', '')[:100]}...")
        print("Status: PASSED")
    else:
        print("Status: FAILED - No metadata returned")
        return False

    # Test 2: Cache hit verification
    print("\n[Test 2] Cache hit verification")
    print("-" * 60)
    metadata_cached = downloader._fetch_bilibili_official_metadata(bvid)

    if metadata_cached == metadata:
        print("Status: PASSED - Cache working correctly")
    else:
        print("Status: FAILED - Cache mismatch")
        return False

    # Test 3: Integrated metadata fetch
    print("\n[Test 3] Integrated metadata fetch (via _fetch_metadata)")
    print("-" * 60)
    try:
        video_metadata = downloader._fetch_metadata(test_url, bvid)
        print(f"Video ID: {video_metadata.video_id}")
        print(f"Platform: {video_metadata.platform}")
        print(f"Title: {video_metadata.title}")
        print(f"Author: {video_metadata.author}")
        print(f"Duration: {video_metadata.duration}")
        print(f"Description length: {len(video_metadata.description)} characters")
        print(f"Description preview: {video_metadata.description[:100]}...")
        print(f"Extra fields: {video_metadata.extra}")
        print("Status: PASSED")
    except Exception as e:
        print(f"Status: FAILED - {e}")
        return False

    # Test 4: Error handling (invalid BV ID)
    print("\n[Test 4] Error handling with invalid BV ID")
    print("-" * 60)
    invalid_metadata = downloader._fetch_bilibili_official_metadata("BV_INVALID_ID")

    if invalid_metadata == {}:
        print("Status: PASSED - Error handled gracefully")
    else:
        print("Status: FAILED - Should return empty dict for invalid ID")
        return False

    print("\n" + "=" * 60)
    print("All Tests PASSED")
    print("=" * 60)
    return True


if __name__ == "__main__":
    success = test_fetch_official_metadata()
    sys.exit(0 if success else 1)
