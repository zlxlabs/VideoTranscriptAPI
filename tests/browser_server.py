"""Isolated HTTP fixture server for real-browser tests of the existing web assets."""

from __future__ import annotations

import argparse
import ast
import json
import mimetypes
import sqlite3
import tempfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from jinja2 import Environment, FileSystemLoader, select_autoescape

ROOT = Path(__file__).resolve().parents[1]
STATIC_DIR = ROOT / "src" / "web" / "static"
TEMPLATE_DIR = ROOT / "src" / "web" / "templates"
VIEWS_SOURCE = ROOT / "src" / "video_transcript_api" / "api" / "routes" / "views.py"
FIXTURE_TOKEN = "browser-fixture-token"
VIEW_TOKEN = "browser-fixture-view"


def read_home_page() -> str:
    """Read the exact HTML constant used by the production `/` route."""
    module = ast.parse(VIEWS_SOURCE.read_text(encoding="utf-8"))
    for node in module.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "_HOME_HTML"
            for target in node.targets
        ):
            return ast.literal_eval(node.value)
    raise RuntimeError("Production home page constant _HOME_HTML was not found")


HOME_HTML = read_home_page()
TEMPLATES = Environment(
    loader=FileSystemLoader(TEMPLATE_DIR),
    autoescape=select_autoescape(["html", "xml"]),
)
TEMPLATES.globals["asset_v"] = "browser-test"
FIXTURE_TRANSCRIPT = "隔离浏览器夹具中的正文，验证公开阅读与导出。"


def initialize_database(db_path: Path) -> None:
    with sqlite3.connect(db_path) as db:
        db.execute(
            "CREATE TABLE history (view_token TEXT PRIMARY KEY, title TEXT NOT NULL, "
            "request_time TEXT NOT NULL, status TEXT NOT NULL, video_url TEXT NOT NULL)"
        )
        db.execute(
            "CREATE TABLE submissions (payload TEXT NOT NULL)"
        )
        seed_database(db)


def seed_database(db: sqlite3.Connection) -> None:
    db.execute("DELETE FROM history")
    db.execute("DELETE FROM submissions")
    db.execute(
        "INSERT INTO history VALUES (?, ?, ?, ?, ?)",
        (
            VIEW_TOKEN,
            "浏览器回归历史样本",
            "2026-10-08T12:00:00",
            "success",
            "https://www.youtube.com/watch?v=fixture",
        ),
    )


class BrowserFixtureHandler(BaseHTTPRequestHandler):
    server_version = "VTA-Browser-Fixture/1.0"

    @property
    def db_path(self) -> Path:
        return self.server.db_path  # type: ignore[attr-defined]

    def log_message(self, format: str, *args: object) -> None:
        return

    def send_bytes(self, status: int, content: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(content)

    def send_json(self, status: int, body: dict[str, object]) -> None:
        self.send_bytes(
            status,
            json.dumps(body, ensure_ascii=False).encode("utf-8"),
            "application/json; charset=utf-8",
        )

    def read_json(self) -> dict[str, object]:
        size = int(self.headers.get("Content-Length", "0"))
        return json.loads(self.rfile.read(size) or b"{}")

    def authorized(self) -> bool:
        return self.headers.get("Authorization") == f"Bearer {FIXTURE_TOKEN}"

    def do_GET(self) -> None:
        parsed = urlsplit(self.path)
        path = parsed.path
        if path == "/__e2e__/ready":
            self.send_bytes(200, b"ready", "text/plain; charset=utf-8")
            return
        if path == "/":
            self.send_bytes(200, HOME_HTML.encode("utf-8"), "text/html; charset=utf-8")
            return
        if path == "/add_task_by_web":
            self.serve_static(STATIC_DIR / "index.html")
            return
        if path == "/static/history.html":
            self.serve_static(STATIC_DIR / "history.html")
            return
        if path == "/sw.js":
            self.serve_static(STATIC_DIR / "sw.js")
            return
        if path.startswith("/static/"):
            relative = Path(path.removeprefix("/static/"))
            asset = (STATIC_DIR / relative).resolve()
            if asset.is_relative_to(STATIC_DIR.resolve()) and asset.is_file():
                self.serve_static(asset)
                return
            self.send_bytes(404, b"not found", "text/plain; charset=utf-8")
            return
        if path.startswith("/view/"):
            query = parse_qs(parsed.query)
            if "raw" in query:
                self.send_bytes(
                    200,
                    ("# 浏览器回归示例\n\n" + FIXTURE_TRANSCRIPT).encode("utf-8"),
                    "text/plain; charset=utf-8",
                )
                return
            if "page" in query:
                self.send_bytes(
                    200,
                    ("<!doctype html><html><body><h1>浏览器回归示例</h1><p>"
                     + FIXTURE_TRANSCRIPT
                     + "</p></body></html>").encode("utf-8"),
                    "text/html; charset=utf-8",
                )
                return
            status = "processing" if path.endswith("processing") else "success"
            if path.endswith("failed"):
                status = "failed"
            elif path.endswith("cleaned"):
                status = "file_cleaned"
            self.serve_view(status)
            return
        if path == "/api/audit/filter-options":
            if not self.authorized():
                self.send_json(401, {"code": 401, "message": "unauthorized"})
                return
            self.send_json(
                200,
                {
                    "code": 200,
                    "data": {
                        "api_key_masked": "browser-…-token",
                        "webhooks": [],
                        "platforms": ["youtube"],
                        "authors": ["浏览器夹具频道"],
                    },
                },
            )
            return
        if path == "/api/audit/history":
            if not self.authorized():
                self.send_json(401, {"code": 401, "message": "unauthorized"})
                return
            with sqlite3.connect(self.db_path) as db:
                rows = db.execute(
                    "SELECT view_token, title, request_time, status, video_url FROM history "
                    "ORDER BY request_time DESC"
                ).fetchall()
            items = [
                {
                    "view_token": row[0],
                    "title": row[1],
                    "request_time": row[2],
                    "status": row[3],
                    "video_url": row[4],
                    "author": "浏览器夹具频道",
                    "platform": "youtube",
                    "wechat_webhook": None,
                }
                for row in rows
            ]
            self.send_json(
                200,
                {
                    "code": 200,
                    "data": {
                        "items": items,
                        "total": len(items),
                        "api_key_masked": "browser-…-token",
                    },
                },
            )
            return
        if path == "/api/audit/summary":
            self.send_json(200, {"code": 200, "data": {"summary": "夹具摘要"}})
            return
        if path == "/robots.txt":
            self.send_bytes(200, b"User-agent: *\n", "text/plain; charset=utf-8")
            return
        if path == "/sitemap.xml":
            self.send_bytes(200, b"<urlset></urlset>", "application/xml")
            return
        if path == "/favicon.ico":
            self.send_bytes(204, b"", "image/x-icon")
            return
        self.send_bytes(404, b"not found", "text/plain; charset=utf-8")

    def do_POST(self) -> None:
        path = urlsplit(self.path).path
        if path == "/__e2e__/reset":
            with sqlite3.connect(self.db_path) as db:
                seed_database(db)
            self.send_json(200, {"ok": True})
            return
        if path == "/api/transcribe":
            if not self.authorized():
                self.send_json(401, {"code": 401, "message": "unauthorized"})
                return
            payload = self.read_json()
            with sqlite3.connect(self.db_path) as db:
                db.execute("INSERT INTO submissions VALUES (?)", (json.dumps(payload),))
            self.send_json(
                200,
                {
                    "code": 202,
                    "data": {"task_id": "browser-fixture-task", "view_token": VIEW_TOKEN},
                },
            )
            return
        self.send_bytes(404, b"not found", "text/plain; charset=utf-8")

    def serve_static(self, path: Path) -> None:
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        if content_type.startswith("text/") or content_type in ("application/javascript", "application/json"):
            content_type += "; charset=utf-8"
        self.send_bytes(200, path.read_bytes(), content_type)

    def serve_view(self, status: str) -> None:
        template_name = {
            "processing": "processing.html",
            "failed": "error.html",
            "file_cleaned": "cleaned.html",
        }.get(status, "transcript.html")
        context: dict[str, object] = {
            "request": None,
            "view_token": VIEW_TOKEN,
            "title": "浏览器回归示例",
            "platform": "YouTube",
            "url": "https://www.youtube.com/watch?v=fixture",
            "status": status,
            "created_at": "2026-10-08T12:00:00",
            "summary_html": '<p>这是原页面模板显示的样例总结。</p>',
            "summary_state": "generated",
            "calibrated_html": f"<p>{FIXTURE_TRANSCRIPT}</p>",
            "calibrated_text": FIXTURE_TRANSCRIPT,
            "stats": {"original_length": len(FIXTURE_TRANSCRIPT), "calibrated_length": len(FIXTURE_TRANSCRIPT), "summary_length": 13},
            "deep_read_prompt_presets": [],
            "page_title": "浏览器回归示例",
            "error_message": "夹具失败状态",
            "message": "夹具失败状态",
        }
        html = TEMPLATES.get_template(template_name).render(**context)
        self.send_bytes(200, html.encode("utf-8"), "text/html; charset=utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", required=True, type=int)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="vta-browser-") as directory:
        db_path = Path(directory) / "fixtures.sqlite3"
        initialize_database(db_path)
        server = ThreadingHTTPServer(("127.0.0.1", args.port), BrowserFixtureHandler)
        server.daemon_threads = True
        server.db_path = db_path  # type: ignore[attr-defined]
        server.serve_forever()


if __name__ == "__main__":
    main()
