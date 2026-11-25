#!/usr/bin/env python3
"""Translation workbench server.

    python3 server.py [PROJECT] [--port 8765]

then open http://localhost:8765. The app always shows the active project
(see tools/tmlib.py); switching projects in the app or with `tm.py use` takes effect at once.
"""
import base64
import json
import mimetypes
import sys
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent / "tools"))
import tmlib  # noqa: E402
from claude_bridge import Bridge  # noqa: E402

BRIDGE = Bridge()

ROOT = Path(__file__).resolve().parent


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=str(ROOT / "app"), **kw)

    def log_message(self, fmt, *args):
        if "/api/version" not in str(args[0] if args else ""):
            super().log_message(fmt, *args)

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def _file(self, path: Path):
        if not path.is_file():
            return self.send_error(404)
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", mimetypes.guess_type(path.name)[0] or "application/octet-stream")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = urlparse(self.path).path
        try:
            if path == "/api/state":
                p = tmlib.paths()
                return self._json({
                    "project": tmlib.active_name(),
                    "projects": [{"name": n, "title": json.loads((tmlib.PROJECTS / n / "project.json")
                                                               .read_text(encoding="utf-8")).get("title", n)}
                                 for n in tmlib.list_projects()],
                    "config": tmlib.load_config(),
                    "source": tmlib.load_source(),
                    "trans": tmlib.load_trans(),
                    "requests": tmlib.load_requests(),
                    "decisions": p.decisions.read_text(encoding="utf-8") if p.decisions.exists() else "",
                    "style": p.style.read_text(encoding="utf-8") if p.style.exists() else "",
                    "version": tmlib.version(),
                    "langs": {k: v[0] for k, v in tmlib.LANGS.items()},
                    "claude": BRIDGE.status(),
                })
            if path == "/api/version":
                return self._json({"version": tmlib.version(), "claude": BRIDGE.status()})
            if path.startswith("/pages/"):
                name = Path(path).name
                return self._file(tmlib.paths().pages / name)
        except SystemExit as e:  # no active project
            return self._json({"error": str(e), "langs": {k: v[0] for k, v in tmlib.LANGS.items()}}, 409)
        if path == "/":
            self.path = "/index.html"
        return super().do_GET()

    def do_POST(self):
        path = urlparse(self.path).path
        b = self._body()
        try:
            if path == "/api/block":
                rec = tmlib.set_block(b["id"], b["text"], "user", b.get("status"))
                return self._json({"ok": True, "block": rec, "version": tmlib.version()})
            if path == "/api/status":
                tmlib.set_status(b["ids"], b["status"])
            elif path == "/api/request":
                return self._json({"ok": True, "request": tmlib.add_request(b)})
            elif path == "/api/request/cancel":
                tmlib.cancel_request(b["id"])
            elif path == "/api/alt/dismiss":
                tmlib.dismiss_alternative(b["id"], b["index"])
            elif path == "/api/decisions":
                tmlib.paths().decisions.write_text(b["text"], encoding="utf-8")
            elif path == "/api/style":
                tmlib.paths().style.write_text(b["text"], encoding="utf-8")
            elif path == "/api/use":
                tmlib.set_active(b["project"])
            elif path == "/api/projects/new":
                if b.get("text"):
                    filename, data = "source.md", b["text"].encode("utf-8")
                else:
                    filename, data = b["filename"], base64.b64decode(b["data"])
                name, msg = tmlib.create_project(
                    b.get("name", ""), filename, data, title=b.get("title", ""), src=b.get("src", "en"),
                    tgt=b.get("tgt", "he"), first=b.get("first"), last=b.get("last"),
                    page_offset=b.get("page_offset", 0), per_page=b.get("per_page", 4))
                return self._json({"ok": True, "project": name, "message": msg})
            elif path == "/api/claude/run":
                task = {"kind": b["kind"]}
                if b["kind"] == "translate":
                    task["pages"] = [int(x) for x in b["pages"]]
                elif b["kind"] == "message":
                    task["text"] = b["text"].strip()
                elif b["kind"] != "requests":
                    raise ValueError("unknown task")
                return self._json({"ok": True, "job": BRIDGE.submit(tmlib.active_name(), task)})
            elif path == "/api/claude/cancel":
                BRIDGE.cancel(int(b["id"]))
            elif path == "/api/settings":
                return self._json({"ok": True, "message": tmlib.update_settings(b)})
            else:
                return self.send_error(404)
            return self._json({"ok": True, "version": tmlib.version()})
        except (KeyError, ValueError, SystemExit, OSError) as e:
            return self._json({"ok": False, "error": str(e)}, 400)


def main():
    args = sys.argv[1:]
    port = 8765
    if "--port" in args:
        i = args.index("--port")
        port = int(args[i + 1])
        del args[i:i + 2]
    if args:
        tmlib.set_active(args[0])
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"Translation workbench: http://localhost:{port}")
    srv.serve_forever()


if __name__ == "__main__":
    main()
