"""Wechat /direct + CDN Range splice tests. HTTP mocked. English only."""
import base64, logging
from unittest.mock import Mock
import pytest, requests
from video_transcript_api.downloaders.media_resolver import MediaResolverDownloader
from video_transcript_api.errors import DownloadFailedError, ResolverAuthError

SPH = "AHaM8SrlXX"
STREAM = f"http://resolver.local:8000/api/stream/wechat_channels/{SPH}"
CDN_A = "https://finder.video.qq.com/secret-token-AAAA1111.mp4"
CDN_B = "https://finder.video.qq.com/secret-token-BBBB2222.mp4"
HEAD = b"\x00\x00\x00\x18ftypisom" + b"\x00" * 8
SECRETS = (CDN_A, CDN_B, "secret-token-AAAA", "secret-token-BBBB")
def _payload(head, rest, cdn=CDN_A):
    body = head + rest
    return {
        "sph_code": SPH, "cdn_url": cdn, "content_length": len(body),
        "encrypted_head_bytes": len(head),
        "head_b64": base64.b64encode(head).decode("ascii"),
    }, body
def _resp(status, start, total, chunks, error=None):
    cr = f"bytes {start}-{start}/*" if total == "*" else f"bytes {start}-{total - 1}/{total}"
    r = Mock(status_code=status, headers={"Content-Range": cr})
    r.close.return_value = None
    if error:
        def _iter(chunk_size=8192):
            yield from chunks
            raise error
        r.iter_content.side_effect = _iter
    else:
        r.iter_content.return_value = list(chunks)
    return r
def _status(code):
    r = Mock(status_code=code, headers={})
    r.iter_content.return_value = []; r.close.return_value = None; return r
def _dl(tmp_path, payloads, monkeypatch, getter=None, max_bytes=0):
    class TM:
        def create_temp_file(self, suffix=".mp4"):
            p = tmp_path / f"w{suffix}"; p.write_bytes(b""); return str(p)
        def untrack_file(self, path):
            return None
    class Client:
        def __init__(self):
            self.calls = []
        def fetch_wechat_direct(self, sph_code):
            self.calls.append(sph_code)
            item = payloads[min(len(self.calls) - 1, len(payloads) - 1)]
            if isinstance(item, Exception):
                raise item
            return item
    dl = MediaResolverDownloader()
    dl._resolver_netloc = "resolver.local:8000"
    dl.client = Client()
    dl.temp_manager = TM()
    if max_bytes:
        dl.max_download_bytes = max_bytes
    monkeypatch.setattr("video_transcript_api.downloaders.media_resolver.validate_url_safe", lambda u: u)
    if getter is not None:
        monkeypatch.setattr(requests, "get", getter)
    return dl

def _no_secret(caplog, exc=None):
    text = " ".join(r.getMessage() for r in caplog.records)
    for s in SECRETS:
        assert s not in text
        if exc is not None:
            assert s not in str(exc)

class TestWechatDirectSplice:
    def test_happy_path_head_plus_one_206(self, monkeypatch, tmp_path, caplog):
        caplog.set_level(logging.DEBUG)
        rest = b"REST-BYTES-OK"
        payload, expected = _payload(HEAD, rest)
        calls = []
        def getter(url, headers=None, stream=True, timeout=None, **kw):
            calls.append((url, dict(headers or {}), timeout, kw.get("allow_redirects")))
            return _resp(206, len(HEAD), len(expected), [rest])
        out = _dl(tmp_path, [payload], monkeypatch, getter).download_file(STREAM, "x.mp4")
        assert open(out, "rb").read() == expected
        assert calls == [(CDN_A, {"Range": f"bytes={len(HEAD)}-"}, (10, 60), False)]
        assert "X-API-Key" not in calls[0][1]
        _no_secret(caplog)

    def test_head_only_does_not_hit_cdn(self, monkeypatch, tmp_path):
        payload, expected = _payload(HEAD, b"")
        getter = lambda *a, **k: pytest.fail("CDN must not be called")
        out = _dl(tmp_path, [payload], monkeypatch, getter).download_file(STREAM, "x.mp4")
        assert open(out, "rb").read() == expected

    def test_interrupt_then_refresh_resumes_range(self, monkeypatch, tmp_path, caplog):
        caplog.set_level(logging.DEBUG)
        rest = b"ABCDEFGHIJKLMNOP"
        first, expected = _payload(HEAD, rest, CDN_A)
        second, _ = _payload(HEAD, rest, CDN_B)
        calls, n = [], {"i": 0}
        def getter(url, headers=None, stream=True, timeout=None, **kw):
            n["i"] += 1
            calls.append((url, (headers or {}).get("Range"), kw.get("allow_redirects")))
            if n["i"] == 1:
                return _resp(206, len(HEAD), len(expected), [rest[:6]],
                             error=requests.exceptions.ConnectionError("cut"))
            return _resp(206, len(HEAD) + 6, len(expected), [rest[6:]])
        dl = _dl(tmp_path, [first, second], monkeypatch, getter)
        assert open(dl.download_file(STREAM, "x.mp4"), "rb").read() == expected
        assert calls[1] == (CDN_B, f"bytes={len(HEAD) + 6}-", False)
        _no_secret(caplog)

    @pytest.mark.parametrize("mode", ["410", "bad_range", "star_total", "wrong_total", "302"])
    def test_cdn_error_then_refresh_succeeds(self, monkeypatch, tmp_path, mode):
        rest = b"BODY"
        first, expected = _payload(HEAD, rest, CDN_A)
        second, _ = _payload(HEAD, rest, CDN_B)
        n = {"i": 0}
        def getter(url, headers=None, stream=True, timeout=None, **kw):
            n["i"] += 1
            n["redir"] = kw.get("allow_redirects")
            if n["i"] > 1:
                return _resp(206, len(HEAD), len(expected), [rest])
            return {
                "410": _status(410), "302": _status(302),
                "star_total": _resp(206, len(HEAD), "*", [b"NO"]),
                "wrong_total": _resp(206, len(HEAD), len(expected) + 99, [b"NO"]),
                "bad_range": _resp(206, 0, len(expected), [b"NO"]),
            }[mode]
        dl = _dl(tmp_path, [first, second], monkeypatch, getter)
        assert open(dl.download_file(STREAM, "x.mp4"), "rb").read() == expected
        assert dl.client.calls == [SPH, SPH] and n["redir"] is False

    @pytest.mark.parametrize("field", ["content_length", "head_b64"])
    def test_refresh_identity_mismatch(self, monkeypatch, tmp_path, field):
        first, _ = _payload(HEAD, b"BODY", CDN_A)
        second, _ = _payload(HEAD, b"BODY", CDN_B)
        second[field] = first[field] + 1 if field == "content_length" else "AAAA"
        with pytest.raises(DownloadFailedError) as ei:
            _dl(tmp_path, [first, second], monkeypatch,
                lambda *a, **k: _status(410)).download_file(STREAM, "x.mp4")
        assert SPH in str(ei.value) and field in str(ei.value)
        assert not (tmp_path / "w.mp4").exists()
        assert all(s not in str(ei.value) for s in SECRETS)

    def test_final_size_mismatch_raises(self, monkeypatch, tmp_path, caplog):
        caplog.set_level(logging.DEBUG)
        payload, _ = _payload(HEAD, b"PARTIAL")
        payload["content_length"] = payload["encrypted_head_bytes"] + 50
        with pytest.raises(DownloadFailedError) as ei:
            _dl(tmp_path, [payload] * 8, monkeypatch, lambda *a, **k: _status(410)).download_file(STREAM, "x.mp4")
        _no_secret(caplog, ei.value)

    @pytest.mark.parametrize("kind", ["oversize", "noftyp", "auth"])
    def test_fail_before_cdn(self, monkeypatch, tmp_path, caplog, kind):
        caplog.set_level(logging.DEBUG)
        getter = lambda *a, **k: pytest.fail("CDN must not be called")
        if kind == "oversize":
            payload, expected = _payload(HEAD, b"X" * 50)
            with pytest.raises(DownloadFailedError):
                _dl(tmp_path, [payload], monkeypatch, getter, max_bytes=len(expected) - 1).download_file(STREAM, "x.mp4")
        elif kind == "noftyp":
            payload, _ = _payload(b"\x00\x00\x00\x18XXXX" + b"\x00" * 16, b"REST")
            with pytest.raises(DownloadFailedError) as ei:
                _dl(tmp_path, [payload], monkeypatch, getter).download_file(STREAM, "x.mp4")
            assert "ftyp" in str(ei.value).lower()
            _no_secret(caplog, ei.value)
        else:
            with pytest.raises(ResolverAuthError) as ei:
                _dl(tmp_path, [ResolverAuthError("no key")], monkeypatch).download_file(STREAM, "x.mp4")
            _no_secret(caplog, ei.value)

    def test_non_wechat_url_does_not_call_direct(self, monkeypatch, tmp_path):
        dl = _dl(tmp_path, [], monkeypatch)
        monkeypatch.setattr("video_transcript_api.downloaders.base.BaseDownloader.download_file", lambda *a, **k: "/tmp/cdn-ok.mp4")
        assert dl.download_file("https://cdn.example.com/v/7123.mp4", "x.mp4") == "/tmp/cdn-ok.mp4"
        assert dl.client.calls == []
