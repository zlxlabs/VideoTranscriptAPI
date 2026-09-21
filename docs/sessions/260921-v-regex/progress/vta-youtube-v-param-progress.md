# vta-youtube-v-param progress (issue #75)

- Deleted fallback rule `r'[?&]v=([a-zA-Z0-9_-]+)'` from PATTERNS['youtube'] (url_parser.py).
- Added regression tests in tests/unit/test_url_parser.py: non-YouTube v= not youtube (parse + extract_platform), 7 YouTube legal forms.
- Red check (rule kept): 4 new v= tests FAILED as expected, 7 legal forms passed.
- Green check (rule deleted): full `uv run --extra dev pytest tests/unit -q` exit 0.
- test_platform_recognition.py untouched: self-held mirror, no sync needed.
