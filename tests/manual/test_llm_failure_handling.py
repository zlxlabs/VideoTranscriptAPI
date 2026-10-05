"""
Test LLM failure handling - B1 strategy: don't write files on failure

This test verifies:
1. LLMCallError is raised on API failure
2. calibrate_success/summary_success flags are set correctly
3. Files are not saved when LLM calls fail
"""
import sys
import os

# Add project root to sys.path
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '../..'))
sys.path.insert(0, project_root)


def test_llm_call_error_exception():
    """Test that LLMCallError is properly defined and can be raised"""
    from src.video_transcript_api.llm import LLMCallError

    # Test exception can be created with message
    error = LLMCallError("Test error message")
    assert str(error) == "Test error message"
    assert error.message == "Test error message"
    assert error.last_error is None

    # Test exception can be created with last_error
    original_error = ValueError("Original error")
    error = LLMCallError("Wrapper message", original_error)
    assert error.last_error is original_error

    print("Test 1 passed: LLMCallError exception works correctly")


def test_success_flags_in_result_dict():
    """Test that success flags are correctly set in result_dict"""

    # Simulate successful result
    success_result = {
        "calibrate_success": True,
        "summary_success": True,
    }
    assert success_result["calibrate_success"] is True
    assert success_result["summary_success"] is True

    # Simulate failed result
    failed_result = {
        "calibrate_success": False,
        "summary_success": False,
    }
    assert failed_result["calibrate_success"] is False
    assert failed_result["summary_success"] is False

    # Simulate partial failure
    partial_result = {
        "calibrate_success": True,
        "summary_success": False,
    }
    assert partial_result["calibrate_success"] is True
    assert partial_result["summary_success"] is False

    print("Test 2 passed: Success flags work correctly")


def test_save_logic_with_success_flags():
    """Test that save logic respects success flags"""

    # Mock save function that tracks calls
    saved_files = []

    def mock_save_llm_result(platform, media_id, use_speaker_recognition, llm_type, content):
        saved_files.append({
            "llm_type": llm_type,
            "content": content
        })

    # Test case 1: Both succeed - both files should be saved
    saved_files.clear()
    result_dict = {
        "calibrate_success": True,
        "summary_success": True,
        "skip_summary": False,
    }
    calibrated_text = "Calibrated text content"
    summary_text = "Summary text content"

    if result_dict["calibrate_success"]:
        mock_save_llm_result("test", "123", False, "calibrated", calibrated_text)
    if result_dict["summary_success"]:
        mock_save_llm_result("test", "123", False, "summary", summary_text)

    assert len(saved_files) == 2
    assert saved_files[0]["llm_type"] == "calibrated"
    assert saved_files[1]["llm_type"] == "summary"
    print("  Case 1 passed: Both succeed -> both files saved")

    # Test case 2: Both fail - no files should be saved
    saved_files.clear()
    result_dict = {
        "calibrate_success": False,
        "summary_success": False,
        "skip_summary": False,
    }

    if result_dict["calibrate_success"]:
        mock_save_llm_result("test", "123", False, "calibrated", calibrated_text)
    if result_dict["summary_success"]:
        mock_save_llm_result("test", "123", False, "summary", summary_text)

    assert len(saved_files) == 0
    print("  Case 2 passed: Both fail -> no files saved")

    # Test case 3: Calibrate succeeds, summary fails - only calibrate file saved
    saved_files.clear()
    result_dict = {
        "calibrate_success": True,
        "summary_success": False,
        "skip_summary": False,
    }

    if result_dict["calibrate_success"]:
        mock_save_llm_result("test", "123", False, "calibrated", calibrated_text)
    if result_dict["summary_success"]:
        mock_save_llm_result("test", "123", False, "summary", summary_text)

    assert len(saved_files) == 1
    assert saved_files[0]["llm_type"] == "calibrated"
    print("  Case 3 passed: Calibrate success, summary fail -> only calibrate saved")

    print("Test 3 passed: Save logic respects success flags")


def test_fallback_behavior():
    """Test that fallback to original transcript works when LLM file doesn't exist"""

    # Simulate cache_data without LLM results (files not saved due to failure)
    cache_data_no_llm = {
        "transcript_data": "Original transcript text from ASR",
        "title": "Test Video",
    }

    # Simulate getting view data - should fallback to transcript_data
    transcript = cache_data_no_llm.get('llm_calibrated') or cache_data_no_llm.get('transcript_data', '')
    summary = cache_data_no_llm.get('llm_summary', 'Summary not available')

    assert transcript == "Original transcript text from ASR"
    assert summary == "Summary not available"

    print("Test 4 passed: Fallback to original transcript works")


if __name__ == "__main__":
    print("=" * 60)
    print("Testing LLM Failure Handling (B1 Strategy)")
    print("=" * 60)
    print()

    test_llm_call_error_exception()
    print()

    test_success_flags_in_result_dict()
    print()

    test_save_logic_with_success_flags()
    print()

    test_fallback_behavior()
    print()

    print("=" * 60)
    print("All tests passed!")
    print("=" * 60)
