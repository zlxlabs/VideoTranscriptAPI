#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""
Test script to verify loguru migration
"""

import os
import sys

# Add src directory to Python path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'src'))

from video_transcript_api.utils import setup_logger

def test_logger():
    """Test the logger functionality"""
    # Initialize logger
    log = setup_logger("test_logger")

    print("\n=== Testing loguru logger ===\n")

    # Test different log levels
    log.debug("This is a debug message")
    log.info("This is an info message")
    log.warning("This is a warning message")
    log.error("This is an error message")

    # Test multiple calls to setup_logger (should not reconfigure)
    log2 = setup_logger("another_logger")
    log2.info("Testing another logger instance (should share same configuration)")

    print("\n=== Logger test completed ===\n")
    print("Check logs/app.log for file output")

    return True

if __name__ == "__main__":
    try:
        success = test_logger()
        if success:
            print("Logger test PASSED")
            sys.exit(0)
        else:
            print("Logger test FAILED")
            sys.exit(1)
    except Exception as e:
        print(f"Logger test FAILED with exception: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
