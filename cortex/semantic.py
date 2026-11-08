"""Optional meaning-based search using a local Ollama embedding model.

If Ollama (https://ollama.com) is running with an embedding model pulled
(default: `ollama pull nomic-embed-text`), Cortex embeds each note in a
background thread and fuses meaning-based matches with keyword matches. A search
for "car repairs" then finds a note about "fixing the automobile".

Nothing leaves your computer: Ollama runs locally. If Ollama is not running,
this module stays silent and keyword search works exactly as before.

Settings (environment variables):
  CORTEX_SEMANTIC=off            disable entirely
  OLLAMA_HOST=http://127.0.0.1:11434
  CORTEX_EMBED_MODEL=nomic-embed-text
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
import threading
import time
import urllib.request
from array import array

try:  # optional speed-up; pure Python is used without it
    import numpy as _np
except ImportError:  # pragma: no cover
    _np = None

EMB_SCHEMA = "CREATE TABLE IF NOT EXISTS emb(note_id INTEGER PRIMARY KEY, hash TEXT, vec BLOB, ts REAL)"
DOC_CHARS = 2400  # title + first part of the note; keeps embedding fast and focused
BATCH = 16
PROBE_TTL = 60.0
MIN_SIM = 0.2
_NO_PROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # Ollama is local


def _normalize(v: list[float]) -> array:
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return array("f", (x / n for x in v))


class Semantic:
    def __init__(self, brain, enabled: bool | None = None):
        self.brain = brain
        self.host = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434").rstrip("/")
        if not self.host.startswith("http"):
            self.host = "http://" + self.host
        self.model = os.environ.get("CORTEX_EMBED_MODEL", "nomic-embed-text")
        off = os.environ.get("CORTEX_SEMANTIC", "").lower() in ("off", "0", "false", "no")
        self.enabled = False if (off or enabled is False) else None  # None = probe when needed
        self._probe_at = -PROBE_TTL
        self._available = False
        self._probing = False
        self._worker: threading.Thread | None = None
        self._pending = False
        self._running = False
        self._kick_lock = threading.Lock()
        self._stop = threading.Event()
        self._cache: tuple[list[int], object] | None = None
        self._cache_key = None
        self.error = ""
        brain.db.execute(EMB_SCHEMA)
        brain.db.commit()
        if self.enabled is None or enabled:
            self.kick()

    # ── availability ─────────────────────────────────────────────────────
    def available(self) -> bool:
        """Never blocks: returns the last known state and re-probes in the background.
        (On Windows a refused localhost connection can take ~2s, which must not stall searches.)"""
        if self.enabled is False:
            return False
        if time.monotonic() - self._probe_at >= PROBE_TTL and not self._probing:
            self._probe_at = time.monotonic()
            self._probing = True
            threading.Thread(target=self._probe, name="cortex-probe", daemon=True).start()
        return self._available

    def _probe(self) -> None:
        was = self._available
        try:
            with _NO_PROXY.open(self.host + "/api/tags", timeout=3) as r:
                models = [m.get("name", "") for m in json.loads(r.read()).get("models", [])]
            self._available = any(m == self.model or m.startswith(self.model + ":") for m in models)
            self.error = "" if self._available else f"run: ollama pull {self.model}"
        except Exception:
            self._available = False
            self.error = "Ollama is not running"
        finally:
            self._probing = False
        if self._available and not was:
            self.kick()

    def status(self) -> dict:
        db = self.brain.db
        total = db.execute("SELECT count(*) FROM notes WHERE path NOT LIKE 'Conversations/%'").fetchone()[0]
        done = db.execute("SELECT count(*) FROM emb e JOIN notes n ON n.id=e.note_id").fetchone()[0]
        on = self.available()
        return {"on": on, "model": self.model, "indexed": done, "total": total,
                "hint": "" if on else (self.error or "optional: install Ollama for meaning-based search")}

    # ── embedding ────────────────────────────────────────────────────────
    def _prefix(self, kind: str) -> str:
        if "nomic" in self.model:
            return "search_query: " if kind == "query" else "search_document: "
        return ""

    def embed(self, texts: list[str], kind: str = "doc") -> list[array]:
        body = json.dumps({"model": self.model, "input": [self._prefix(kind) + t for t in texts]}).encode()
        req = urllib.request.Request(self.host + "/api/embed", data=body, headers={"Content-Type": "application/json"})
        with _NO_PROXY.open(req, timeout=60) as r:
            vecs = json.loads(r.read())["embeddings"]
        return [_normalize(v) for v in vecs]

    def kick(self) -> None:
        """Embed new/changed notes in the background (no-op if Ollama is absent)."""
        with self._kick_lock:  # the check and the hand-off are atomic: no lost wake-ups
            if self._running:
                self._pending = True
                return
            if not self.available():
                return
            self._pending = False
            self._running = True
        self._worker = threading.Thread(target=self._run, name="cortex-embed", daemon=True)
        self._worker.start()

    def _run(self) -> None:
        db = sqlite3.connect(self.brain.local_dir / "index.db", timeout=15)
        try:
            while not self._stop.is_set():
                # Imported conversations are left to keyword search: they are huge and
                # would dominate memory (each embedding is ~3 KB) and query time.
                rows = db.execute(
                    "SELECT n.id, n.title, n.path, e.hash FROM notes n LEFT JOIN emb e ON e.note_id=n.id "
                    "WHERE n.path NOT LIKE 'Conversations/%'").fetchall()
                todo = []
                for nid, title, path, old in rows:
                    body = self._body(path)
                    doc = f"{title}\n{re.sub(r'\s+', ' ', body)[:DOC_CHARS]}"
                    h = hashlib.sha1(doc.encode("utf-8", "replace")).hexdigest()[:16]
                    if h != old:
                        todo.append((nid, h, doc))
                if not todo:
                    with self._kick_lock:
                        if not self._pending:
                            self._running = False
                            return
                        self._pending = False
                    continue
                for i in range(0, len(todo), BATCH):
                    if self._stop.is_set():
                        return
                    chunk = todo[i:i + BATCH]
                    try:
                        vecs = self.embed([d for _, _, d in chunk])
                    except Exception as e:  # Ollama went away mid-run
                        self.error = f"embedding failed: {e}"
                        self._probe_at = -PROBE_TTL
                        return
                    now = time.time()
                    db.executemany("INSERT OR REPLACE INTO emb(note_id,hash,vec,ts) VALUES(?,?,?,?)",
                                   [(nid, h, v.tobytes(), now) for (nid, h, _), v in zip(chunk, vecs)])
                    db.commit()
                    self._cache = None
        finally:
            with self._kick_lock:
                self._running = False
            db.close()

    def _body(self, path: str) -> str:
        from .core import split_frontmatter
        try:
            with open(self.brain.root / path, encoding="utf-8", errors="replace") as f:
                return split_frontmatter(f.read(DOC_CHARS * 3))[1]
        except OSError:
            return ""

    def drop(self, note_id: int) -> None:
        self.brain.db.execute("DELETE FROM emb WHERE note_id=?", (note_id,))
        self._cache = None

    def stop(self) -> None:
        self._stop.set()

    # ── querying ─────────────────────────────────────────────────────────
    def _matrix(self):
        db = self.brain.db
        # Every (re)embed stamps ts, and deletes change the count: together they detect any change.
        key = tuple(db.execute("SELECT count(*), coalesce(max(ts),0) FROM emb").fetchone())
        if self._cache is not None and self._cache_key == key:
            return self._cache
        ids, vecs = [], []
        for nid, blob in db.execute("SELECT e.note_id, e.vec FROM emb e JOIN notes n ON n.id=e.note_id"):
            a = array("f")
            a.frombytes(blob)
            ids.append(nid)
            vecs.append(a)
        mat = (_np.array(vecs, dtype=_np.float32) if _np is not None and vecs else vecs)
        self._cache, self._cache_key = (ids, mat), key
        return self._cache

    def _rank(self, qv: array, k: int, exclude: int | None = None) -> list[tuple[int, float]]:
        ids, mat = self._matrix()
        if not ids:
            return []
        if _np is not None:
            sims = (mat @ _np.frombuffer(qv.tobytes(), dtype=_np.float32)).tolist()
        else:
            sims = [sum(map(float.__mul__, v, qv)) if len(v) == len(qv) else 0.0 for v in mat]
        pairs = [(i, s) for i, s in zip(ids, sims) if s >= MIN_SIM and i != exclude]
        pairs.sort(key=lambda x: -x[1])
        return pairs[:k]

    def _hits(self, pairs):
        from .core import MD_NOISE, Hit
        out = []
        for nid, sim in pairs:
            r = self.brain.db.execute("SELECT path, title FROM notes WHERE id=?", (nid,)).fetchone()
            if r:
                snip = " ".join(MD_NOISE.sub("", self._body(r["path"])[:400]).split())[:170]
                out.append(Hit(r["path"], r["title"], snip, round(sim, 4), ["meaning"]))
        return out

    def search(self, q: str, k: int = 20):
        if not self.available() or not self._matrix()[0]:
            return []
        try:
            qv = self.embed([q], kind="query")[0]
        except Exception as e:
            self.error = f"embedding failed: {e}"
            self._probe_at = -PROBE_TTL
            return []
        return self._hits(self._rank(qv, k))

    def similar(self, path: str, k: int = 6):
        if self.enabled is False:
            return []
        row = self.brain.db.execute(
            "SELECT e.note_id, e.vec FROM emb e JOIN notes n ON n.id=e.note_id WHERE n.path=?", (path,)).fetchone()
        if not row:
            return []
        qv = array("f")
        qv.frombytes(row[1])
        return self._hits(self._rank(qv, k, exclude=row[0]))
