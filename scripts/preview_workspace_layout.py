"""Read-only synthetic layout preview; never imports app, DB, secrets or workers.

Run with .venv/bin/python scripts/preview_workspace_layout.py.
Only loopback is bound; POST/PATCH/DELETE are rejected. Stop after visual QA.
"""
import argparse
import mimetypes
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

from jinja2 import Environment, FileSystemLoader, select_autoescape

ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = Environment(loader=FileSystemLoader(ROOT / "templates"), autoescape=select_autoescape())


def preview_html():
    html = TEMPLATES.get_template("index.html").render(
        current_user=SimpleNamespace(username="布局预览", role="admin", chat_nav_hidden=False),
        account_level="lv3",
        url_for=lambda endpoint, filename=None: f"/static/{filename}" if filename else "/preview-manifest.json",
    )
    return html.replace(
        "<body>",
        '<body><script src="/layout-fixtures.js"></script>'
        '<div style="position:fixed;bottom:8px;left:8px;z-index:99999;pointer-events:none;'
        'padding:5px 8px;background:#fff2d2;border:1px solid #e2bc64;border-radius:6px;'
        'font:11px system-ui;color:#765511">隔离布局预览 · 合成数据 · 禁止写入</div>',
        1,
    ).encode()


class PreviewHandler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_GET(self):
        path = urlsplit(self.path).path
        if path == "/":
            content, mime = preview_html(), "text/html; charset=utf-8"
        elif path == "/layout-fixtures.js":
            content = (ROOT / "tests/fixtures/workspace_layout_preview.js").read_bytes()
            mime = "text/javascript; charset=utf-8"
        elif path.startswith("/static/"):
            target = (ROOT / path.lstrip("/")).resolve()
            if not target.is_relative_to(ROOT / "static") or not target.is_file():
                self.send_error(404)
                return
            content, mime = target.read_bytes(), mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        elif path == "/preview-manifest.json":
            content, mime = b"{}", "application/json"
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(content)

    def reject_write(self):
        self.send_error(405, "Synthetic layout preview is read-only")

    do_POST = reject_write
    do_PATCH = reject_write
    do_DELETE = reject_write
    do_PUT = reject_write


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=0)
    args = parser.parse_args()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), PreviewHandler)
    print(f"Synthetic read-only layout preview: http://127.0.0.1:{server.server_port}/", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
