"""Cortex app host: a small local web server with a JSON API and the UI.

Bound to 127.0.0.1 only. Write requests need the `X-Cortex` header, which a
browser cannot add to a cross-site request without a CORS preflight that this
server never approves, so other websites cannot write to your brain.
"""

from __future__ import annotations

import json
import os
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
            try:
                data = json.loads(self.rfile.read(n) or b"{}") if n else {}
            except ValueError:
                raise ValueError("request body must be JSON")
            if not isinstance(data, dict):
                raise ValueError("request body must be a JSON object")
            return data

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
            except (TypeError, AttributeError) as e:  # a field of the wrong type (e.g. a number where text belongs)
                self._json({"error": f"invalid request: {e}"}, 400)
            except OSError as e:  # disk full, file locked by another program, etc.
                self._json({"error": f"couldn't complete that: {e.strerror or e}"}, 500)
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
                "/api/recent": lambda: brain.recent(_int(q.get("limit"), 20)),
                "/api/tags": lambda: brain.tags(),
                "/api/graph": lambda: brain.graph(),
                "/api/activity": lambda: brain.activity(_int(q.get("limit"), 60), q.get("agent") or None),
                "/api/search": lambda: [h.__dict__ for h in brain.search(q.get("q", ""), _int(q.get("limit"), 30), q.get("tag") or None)],
                "/api/note": lambda: brain.read(q["path"]),
                "/api/related": lambda: [h.__dict__ for h in brain.related(q["path"], _int(q.get("limit"), 6))],
                "/api/tasks": lambda: brain.tasks(q.get("status", "open"), q.get("path") or None, q.get("q") or None, _int(q.get("limit"), 500)),
                "/api/health": lambda: brain.health(),
                "/api/recap": lambda: brain.recap(_int(q.get("days"), 7)),
                "/api/history": lambda: brain.history(q["path"]),
                "/api/version": lambda: {"content": brain.version(q["path"], q["id"])},
                "/api/import/status": lambda: Importer(brain).status(),
                "/api/sync": lambda: _cloud().status(brain, _cfg()),
                "/api/setup": lambda: {"import_history": _cfg().get("import_history"),
                                       "connected": bool(_cfg().get("connected_at")),
                                       "detected": Importer(brain).status()["detected"]},
                "/api/conversations": lambda: brain.recent(_int(q.get("limit"), 200), conversations="only") if not q.get("q")
                    else [h.__dict__ for h in brain.search(q["q"], _int(q.get("limit"), 50), scope="conversations")],
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
                    self._json(brain.toggle_task(b["path"], _int(b["line"], -1, lo=-1), b.get("text"), agent="you"))
                elif u.path == "/api/import":
                    from .cli import import_allowed
                    export = b.get("file")
                    if not export and not import_allowed():
                        raise PermissionError("turn on 'Import my AI history' first")
                    if export:
                        p = Path(str(export).strip().strip('"')).expanduser()
                        try:
                            ok = p.is_file()
                        except OSError:
                            ok = False
                        if not ok:
                            raise FileNotFoundError(f"no file at {p}")
                        self._json(Importer(brain).run(export=p))
                    else:
                        self._json(Importer(brain).run())
                elif u.path == "/api/settings":
                    from .cli import save_config
                    cfg = _cfg()
                    if "import_history" in b:
                        cfg["import_history"] = bool(b["import_history"])
                    save_config(cfg)
                    self._json({"import_history": cfg.get("import_history")})
                elif u.path == "/api/connect":
                    from .cli import connect_hosts
                    self._json(connect_hosts(brain.root))
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
                    self._json(_cloud().backup(brain.root, Path(cfg["backup_dir"]), keep=_int(cfg.get("backup_keep"), 30, lo=1)))
                elif u.path == "/api/research":
                    from . import research as R
                    srcs = b.get("sources") or list(R.DEFAULT_SOURCES)
                    self._json(R.search(b["query"], srcs, _int(b.get("per_source"), 6)))
                elif u.path == "/api/fetch":
                    from . import research as R
                    self._json(R.fetch(b["url"], _int(b.get("max_chars"), 30000)))
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


def _int(v, default: int, lo: int = 0, hi: int = 100_000) -> int:
    """Query/body numbers from the browser, clamped to a sane range (huge values overflow SQLite)."""
    try:
        n = int(float(v)) if v not in (None, "") else default
    except (TypeError, ValueError, OverflowError):
        n = default
    return max(lo, min(n, hi))


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
                r = _cloud().backup(brain.root, Path(cfg["backup_dir"]), keep=_int(cfg.get("backup_keep"), 30, lo=1))
                brain.log("cortex", "backed up", "", f"{r['files']} files to {Path(r['path']).name}")
                print(f"Backup saved: {r['path']}")
        except Exception as e:
            print(f"Backup skipped: {e}")
        stop.wait(3600)


def _auto_import(brain: Brain, stop: threading.Event) -> None:
    """Keep conversations from every AI app flowing in while the app is open."""
    from .cli import import_allowed
    while not stop.is_set():
        if not import_allowed():  # nothing is read until the user says yes in the app
            stop.wait(15)
            continue
        try:
            r = Importer(brain).run()
            if r.get("new") or r.get("updated"):
                print(f"Imported conversations: {r['new']} new, {r['updated']} updated")
        except Exception as e:
            print(f"Conversation import skipped: {e}")
        stop.wait(IMPORT_EVERY)


class _Server(ThreadingHTTPServer):
    # On Windows, SO_REUSEADDR (Python's default for HTTP servers) lets a second program bind a
    # port that is already in use, so two servers would silently share it. Exclusive on Windows.
    allow_reuse_address = os.name != "nt"
    daemon_threads = True


def _running_cortex(port: int) -> bool:
    try:
        opener = __import__("urllib.request").request.build_opener(__import__("urllib.request").request.ProxyHandler({}))
        with opener.open(f"http://127.0.0.1:{port}/api/setup", timeout=1.5) as r:
            return "import_history" in json.loads(r.read())
    except Exception:
        return False


def _bind(brain: Brain, port: int):
    """Use the preferred port; if Cortex already runs there return (None, port); if some other
    program holds it, take the next free port."""
    for p in range(port, port + 20):
        try:
            return _Server(("127.0.0.1", p), make_handler(brain)), p
        except OSError:
            if _running_cortex(p):
                return None, p
    raise OSError(f"no free port between {port} and {port + 19}")


def run(vault: str, port: int = 7777, open_browser: bool = True, auto_import: bool = True) -> None:
    brain = Brain(vault)
    info = brain.sync(force=True)
    if auto_import:
        threading.Thread(target=_auto_import, args=(brain, threading.Event()), name="cortex-import", daemon=True).start()
    threading.Thread(target=_auto_backup, args=(brain, threading.Event()), name="cortex-backup", daemon=True).start()
    prov = _cloud().provider_for(brain.root)
    if prov:
        print(f"Cloud: this brain lives in {prov['provider']} (search index kept locally at {brain.local_dir})")
    httpd, port = _bind(brain, port)
    url = f"http://127.0.0.1:{port}"
    if httpd is None:  # Cortex is already open: just show it
        print(f"Cortex is already running at {url}; opening it.")
        if open_browser:
            webbrowser.open(url)
        brain.close()
        return
    from .notify import attach
    if attach(brain):
        print("Changes are also sent to the local app at CORTEX_NOTIFY_URL.")
    print(f"Cortex is running at {url}")
    print(f"Vault: {brain.root}  ({info['notes']} notes indexed)")
    print("Press Ctrl+C to stop.")
    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
