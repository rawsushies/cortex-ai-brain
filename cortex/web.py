"""Cortex app host: a small local web server with a JSON API and the UI.

Bound to 127.0.0.1 only. Write requests need the `X-Cortex` header, which a
browser cannot add to a cross-site request without a CORS preflight that this
server never approves, so other websites cannot write to your brain.
"""

from __future__ import annotations

import json
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .core import Brain, ConflictError
from .importers import Importer
from .research import FetchError

UI_DIR = Path(__file__).parent / "ui"
MAX_BODY = 25_000_000
# Scripts only from the app itself: even if note text slipped past escaping, no inline
# script or event handler would run, and nothing can be sent to another site.
CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
       "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'; object-src 'none'")
TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
         ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml"}


def make_handler(brain: Brain):
    class Handler(BaseHTTPRequestHandler):
        server_version = "Cortex/1.0"

        def log_message(self, *_):  # keep the console quiet
            pass

        # ── helpers ──────────────────────────────────────────────────────
        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Cross-Origin-Resource-Policy", "same-origin")
            self.send_header("Content-Security-Policy", CSP)
            self.end_headers()
            self.wfile.write(body)

        def _allowed(self) -> bool:
            """Block DNS rebinding and cross-site requests before anything else.

            DNS rebinding: a web page on evil.example re-points its domain to 127.0.0.1 and
            then talks to Cortex as if same-origin (reading and writing your notes). The
            browser still sends Host: evil.example, so only our own host names are accepted.
            Sec-Fetch-Site: modern browsers mark requests a different site triggered."""
            port = self.server.server_port
            host = (self.headers.get("Host") or "").lower()
            if host not in {f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"}:
                self._json({"error": "forbidden host"}, 403)
                return False
            site = self.headers.get("Sec-Fetch-Site")
            if site and site not in ("same-origin", "none"):
                self._json({"error": "cross-site request refused"}, 403)
                return False
            return True

        def _json(self, obj, code: int = 200) -> None:
            self._send(code, json.dumps(obj, ensure_ascii=False).encode(), "application/json; charset=utf-8")

        def _body(self) -> dict:
            n = int(self.headers.get("Content-Length") or 0)
            if n > MAX_BODY:
                raise ValueError(f"request too large (limit {MAX_BODY // 1_000_000} MB)")
            return json.loads(self.rfile.read(n) or b"{}") if n else {}

        def _guard(self) -> bool:
            if self.headers.get("X-Cortex") != "1":
                self._json({"error": "missing X-Cortex header"}, 403)
                return False
            return True

        def _route(self, fn) -> None:
            try:
                fn()
            except FileNotFoundError as e:
                self._json({"error": str(e)}, 404)
            except ConflictError as e:
                self._json({"error": str(e), "conflict": True}, 409)
            except FetchError as e:
                self._json({"error": str(e)}, 502)
            except PermissionError as e:
                self._json({"error": str(e)}, 403)
            except (ValueError, KeyError, FileExistsError) as e:
                self._json({"error": str(e)}, 400)
            except Exception as e:  # pragma: no cover
                self._json({"error": f"server error: {e}"}, 500)

        # ── GET ──────────────────────────────────────────────────────────
        def do_GET(self) -> None:
            if not self._allowed():
                return
            u = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            api = {
                "/api/stats": lambda: brain.stats(),
                "/api/tree": lambda: brain.tree(),
                "/api/recent": lambda: brain.recent(int(q.get("limit", 20))),
                "/api/tags": lambda: brain.tags(),
                "/api/graph": lambda: brain.graph(),
                "/api/activity": lambda: brain.activity(int(q.get("limit", 60)), q.get("agent") or None),
                "/api/search": lambda: [h.__dict__ for h in brain.search(q.get("q", ""), int(q.get("limit", 30)), q.get("tag") or None)],
                "/api/note": lambda: brain.read(q["path"]),
                "/api/related": lambda: [h.__dict__ for h in brain.related(q["path"], int(q.get("limit", 6)))],
                "/api/tasks": lambda: brain.tasks(q.get("status", "open"), q.get("path") or None, q.get("q") or None, int(q.get("limit", 500))),
                "/api/health": lambda: brain.health(),
                "/api/recap": lambda: brain.recap(int(q.get("days", 7))),
                "/api/history": lambda: brain.history(q["path"]),
                "/api/version": lambda: {"content": brain.version(q["path"], q["id"])},
                "/api/import/status": lambda: Importer(brain).status(),
                "/api/sync": lambda: _cloud().status(brain, _cfg()),
                "/api/conversations": lambda: brain.recent(int(q.get("limit", 200)), conversations="only") if not q.get("q")
                    else [h.__dict__ for h in brain.search(q["q"], int(q.get("limit", 50)), scope="conversations")],
            }
            if u.path in api:
                return self._route(lambda: self._json(api[u.path]()))
            name = "index.html" if u.path in ("/", "") else u.path.lstrip("/")
            f = (UI_DIR / name).resolve()
            if UI_DIR.resolve() in f.parents and f.is_file():
                return self._send(200, f.read_bytes(), TYPES.get(f.suffix, "application/octet-stream"))
            self._json({"error": "not found"}, 404)

        # ── POST / DELETE ────────────────────────────────────────────────
        def do_POST(self) -> None:
            if not self._allowed() or not self._guard():
                return
            u = urlparse(self.path)

            def go():
                b = self._body()
                if u.path == "/api/note":
                    self._json(brain.write(b["path"], b.get("content", ""), agent="you", mode=b.get("mode", "overwrite"),
                                           expect_mtime=b.get("expect_mtime")))
                elif u.path == "/api/move":
                    self._json(brain.move(b["path"], b["to"], agent="you"))
                elif u.path == "/api/restore":
                    self._json(brain.restore(b["path"], b["id"], agent="you"))
                elif u.path == "/api/task":
                    self._json(brain.add_task(b["text"], b.get("path") or None, b.get("due") or None, agent="you"))
                elif u.path == "/api/task/toggle":
                    self._json(brain.toggle_task(b["path"], int(b["line"]), b.get("text"), agent="you"))
                elif u.path == "/api/import":
                    export = b.get("file")
                    if export:
                        p = Path(export.strip().strip('"')).expanduser()
                        if not p.is_file():
                            raise FileNotFoundError(f"no file at {p}")
                        self._json(Importer(brain).run(export=p))
                    else:
                        self._json(Importer(brain).run())
                elif u.path == "/api/backup":
                    cfg = _cfg()
                    if b.get("dir"):
                        d = Path(str(b["dir"]).strip().strip('"')).expanduser()
                        if not d.is_absolute():
                            raise ValueError("give a full folder path, e.g. G:\\My Drive\\Cortex Backups")
                        from .cli import save_config
                        cfg["backup_dir"], cfg["auto_backup"] = str(d), True
                        save_config(cfg)
                    if not cfg.get("backup_dir"):
                        raise ValueError("choose a backup folder first")
                    self._json(_cloud().backup(brain.root, Path(cfg["backup_dir"]), keep=int(cfg.get("backup_keep", 30))))
                elif u.path == "/api/research":
                    from . import research as R
                    srcs = b.get("sources") or list(R.DEFAULT_SOURCES)
                    self._json(R.search(b["query"], srcs, int(b.get("per_source", 6))))
                elif u.path == "/api/fetch":
                    from . import research as R
                    self._json(R.fetch(b["url"], int(b.get("max_chars", 30000))))
                elif u.path == "/api/research/save":
                    from datetime import date
                    from . import research as R
                    from .importers import _safe
                    if b.get("page"):
                        p = b["page"]
                        self._json(brain.write(f"Resources/Sources/{_safe(p['title'], 80)}.md", R.page_markdown(p), agent="you"))
                    else:
                        self._json(brain.write(f"Resources/Research/{date.today().isoformat()} {_safe(b['query'])}.md",
                                               R.results_markdown(b["query"], b["data"]), agent="you"))
                elif u.path == "/api/capture":
                    self._json(brain.capture(b["text"], agent="you"))
                elif u.path == "/api/reindex":
                    self._json(brain.sync(force=True))
                else:
                    self._json({"error": "not found"}, 404)

            self._route(go)

        def do_DELETE(self) -> None:
            if not self._allowed() or not self._guard():
                return
            u = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            if u.path == "/api/note":
                return self._route(lambda: self._json(brain.delete(q["path"], agent="you")))
            self._json({"error": "not found"}, 404)

    return Handler


IMPORT_EVERY = 300  # seconds


def _cfg() -> dict:
    from .cli import load_config
    return load_config()


def _cloud():
    from . import cloud
    return cloud


def _auto_backup(brain: Brain, stop: threading.Event) -> None:
    """One verified snapshot a day (if a backup folder is set), checked hourly."""
    stop.wait(60)  # let the app settle first
    while not stop.is_set():
        try:
            cfg = _cfg()
            if _cloud().backup_due(cfg):
                r = _cloud().backup(brain.root, Path(cfg["backup_dir"]), keep=int(cfg.get("backup_keep", 30)))
                brain.log("cortex", "backed up", "", f"{r['files']} files to {Path(r['path']).name}")
                print(f"Backup saved: {r['path']}")
        except Exception as e:
            print(f"Backup skipped: {e}")
        stop.wait(3600)


def _auto_import(brain: Brain, stop: threading.Event) -> None:
    """Keep conversations from every AI app flowing in while the app is open."""
    while not stop.is_set():
        try:
            r = Importer(brain).run()
            if r.get("new") or r.get("updated"):
                print(f"Imported conversations: {r['new']} new, {r['updated']} updated")
        except Exception as e:
            print(f"Conversation import skipped: {e}")
        stop.wait(IMPORT_EVERY)


def run(vault: str, port: int = 7777, open_browser: bool = True, auto_import: bool = True) -> None:
    brain = Brain(vault)
    info = brain.sync(force=True)
    if auto_import:
        threading.Thread(target=_auto_import, args=(brain, threading.Event()), name="cortex-import", daemon=True).start()
    threading.Thread(target=_auto_backup, args=(brain, threading.Event()), name="cortex-backup", daemon=True).start()
    prov = _cloud().provider_for(brain.root)
    if prov:
        print(f"Cloud: this brain lives in {prov['provider']} (search index kept locally at {brain.local_dir})")
    httpd = ThreadingHTTPServer(("127.0.0.1", port), make_handler(brain))
    url = f"http://127.0.0.1:{port}"
    print(f"Cortex is running at {url}")
    print(f"Vault: {brain.root}  ({info['notes']} notes indexed)")
    print("Press Ctrl+C to stop.")
    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
