"""ASR monitor WebSocket probe tests.

Verify that ``ASRMonitor.check_service`` performs a real WebSocket handshake
rather than a bare TCP connect/close. The bare-TCP behaviour causes the remote
``websockets`` server to log ``InvalidMessage: did not receive a valid HTTP
request`` every check interval (every 5 minutes), polluting the operator's
error log. A real handshake closes cleanly and also catches the case where TCP
is open but the WS server is dead.

All console output is English only (no emoji, no Chinese).
"""

import asyncio
import logging
import socket
import threading
import time

import pytest

from video_transcript_api.utils.asr_monitor import ASRMonitor


def _make_monitor():
    return ASRMonitor(
        services={"Test": "ws://placeholder"},
        check_interval=1,
        failure_threshold=3,
        debounce_seconds=60,
        notifier=None,
    )


# ---------------------------------------------------------------------------
# Fixture: a real in-process WebSocket server (background thread + event loop)
# ---------------------------------------------------------------------------
@pytest.fixture
def ws_server():
    """Start a real WebSocket server on 127.0.0.1 in a daemon thread.

    Yields ``(host, port, captured_records)`` where ``captured_records`` is a
    list of WARNING+ log records emitted by the ``websockets`` server logger
    during the test. Tests use this to assert the probe does NOT cause server-
    side handshake errors (the whole point of the fix).
    """
    import websockets

    host = "127.0.0.1"
    ready = threading.Event()
    state: dict = {"port": None, "loop": None, "stop_event": None}

    # Capture WARNING+ from the websockets server logger.
    captured: list[logging.LogRecord] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:  # type: ignore[override]
            captured.append(record)

    handler = _Capture()
    handler.setLevel(logging.WARNING)
    ws_logger = logging.getLogger("websockets.server")
    prev_level = ws_logger.level
    ws_logger.addHandler(handler)
    ws_logger.setLevel(logging.WARNING)

    async def _handler(websocket):
        # Drain until client closes; swallow any exception so the test handler
        # itself never produces noise.
        try:
            async for _ in websocket:
                pass
        except Exception:
            pass

    async def _serve():
        async with websockets.serve(_handler, host, 0) as server:
            # server.sockets is available on both legacy and asyncio.server.Server
            sockets = list(getattr(server, "sockets", []) or [])
            if not sockets:
                # Fallback for v13 asyncio.server.Server: underlying asyncio server
                underlying = getattr(server, "server", None)
                sockets = list(getattr(underlying, "sockets", []) or [])
            assert sockets, "no listening socket exposed by websockets.serve"
            state["port"] = sockets[0].getsockname()[1]
            stop_event = asyncio.Event()
            state["stop_event"] = stop_event
            ready.set()
            await stop_event.wait()

    def _run():
        loop = asyncio.new_event_loop()
        state["loop"] = loop
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(_serve())
        finally:
            loop.close()

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    assert ready.wait(timeout=5), "WS server failed to start"

    try:
        yield {"host": host, "port": state["port"], "errors": captured}
    finally:
        loop = state["loop"]
        stop_event = state["stop_event"]
        if loop is not None and stop_event is not None:
            loop.call_soon_threadsafe(stop_event.set)
        thread.join(timeout=5)
        ws_logger.removeHandler(handler)
        ws_logger.setLevel(prev_level)


# ---------------------------------------------------------------------------
# Fixture: a plain TCP server that accepts but never speaks WebSocket
# ---------------------------------------------------------------------------
@pytest.fixture
def tcp_only_server():
    """Bare TCP listener: accept and immediately close, no WS handshake.

    Used to assert the probe does NOT false-positive on a TCP-open-but-dead
    service (the protocol-layer guarantee the new probe adds).
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    sock.listen(5)
    port = sock.getsockname()[1]
    stop = threading.Event()

    def _accept_loop():
        sock.settimeout(0.5)
        while not stop.is_set():
            try:
                conn, _ = sock.accept()
            except (socket.timeout, OSError):
                continue
            try:
                # Read briefly so client TCP send completes, then close without
                # any HTTP response.
                conn.settimeout(0.2)
                try:
                    conn.recv(4096)
                except Exception:
                    pass
            finally:
                try:
                    conn.close()
                except Exception:
                    pass

    thread = threading.Thread(target=_accept_loop, daemon=True)
    thread.start()

    try:
        yield ("127.0.0.1", port)
    finally:
        stop.set()
        try:
            sock.close()
        except Exception:
            pass
        thread.join(timeout=2)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------
class TestWebSocketProbe:
    """``check_service`` must succeed iff a real WebSocket handshake succeeds."""

    def test_probe_succeeds_against_real_ws_server(self, ws_server):
        monitor = _make_monitor()
        url = f"ws://{ws_server['host']}:{ws_server['port']}"
        assert monitor.check_service("Test", url) is True

    def test_probe_does_not_pollute_server_logs(self, ws_server):
        """The whole reason this fix exists: server log must stay clean."""
        monitor = _make_monitor()
        url = f"ws://{ws_server['host']}:{ws_server['port']}"
        monitor.check_service("Test", url)
        # Allow the server a moment to flush any log on the connection-close path.
        time.sleep(0.2)

        offending = [
            r for r in ws_server["errors"]
            if "InvalidMessage" in r.getMessage()
            or "did not receive a valid HTTP request" in r.getMessage()
            or "opening handshake failed" in r.getMessage()
        ]
        assert offending == [], (
            "WS server emitted handshake-error logs during probe: "
            + repr([r.getMessage() for r in offending])
        )

    def test_probe_fails_against_tcp_only_server(self, tcp_only_server):
        """A port that accepts TCP but cannot WS-handshake must read as unhealthy."""
        host, port = tcp_only_server
        monitor = _make_monitor()
        # Bare TCP open but no WS handshake -> protocol-layer probe rejects it.
        assert monitor.check_service("Test", f"ws://{host}:{port}") is False

    def test_probe_fails_when_unreachable(self):
        """Closed port must read as unhealthy and must not raise."""
        monitor = _make_monitor()
        # 127.0.0.1:1 is reserved/closed on virtually every host.
        assert monitor.check_service("Test", "ws://127.0.0.1:1") is False
