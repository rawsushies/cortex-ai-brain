"""A persistent, retryable ingestion queue (machine-local SQLite; never inside the synced vault).

enqueue() is idempotent on event_id. drain() hands pending events to a delivery function in batches;
failures are retried with exponential backoff and, after MAX_ATTEMPTS, parked as 'dead' with the last
error (never deleted: `cortex gateway retry --dead` puts them back). Anything not yet delivered when
the process stops is still there on the next start.

It also remembers per-source read cursors (file path -> byte offset), so adapters read only what's new.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

MAX_ATTEMPTS = 8
BASE_DELAY = 2.0  # seconds; doubles each attempt (2, 4, 8 … ~4 min)

SCHEMA = """
CREATE TABLE IF NOT EXISTS events(
  event_id TEXT PRIMARY KEY, payload TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending',
  attempts INTEGER NOT NULL DEFAULT 0, next_at REAL NOT NULL DEFAULT 0, last_error TEXT DEFAULT '',
  created REAL NOT NULL, delivered REAL, result TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS events_state ON events(state, next_at);
CREATE TABLE IF NOT EXISTS cursors(source TEXT PRIMARY KEY, offset INTEGER NOT NULL, sig TEXT, updated REAL);
CREATE TABLE IF NOT EXISTS invalid(id INTEGER PRIMARY KEY, received REAL, reasons TEXT, payload TEXT);
"""


class Queue:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.db = sqlite3.connect(path, timeout=30, check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        self.db.commit()
        self._lock = threading.Lock()

    def close(self) -> None:
        self.db.close()

    # ── events ───────────────────────────────────────────────────────────
    def enqueue(self, events: list[dict]) -> int:
        """Add normalized, valid events; returns how many were new (already-known ids are ignored)."""
        now = time.time()
        with self._lock:
            before = self.db.total_changes
            self.db.executemany("INSERT OR IGNORE INTO events(event_id,payload,created) VALUES(?,?,?)",
                                [(e["event_id"], json.dumps(e, ensure_ascii=False), now) for e in events])
            self.db.commit()
            return self.db.total_changes - before

    def reject(self, payload, reasons: list[str]) -> None:
        """Keep invalid input (with why) instead of dropping it, so it can be inspected and fixed."""
        with self._lock:
            self.db.execute("INSERT INTO invalid(received,reasons,payload) VALUES(?,?,?)",
                            (time.time(), json.dumps(reasons), json.dumps(payload, ensure_ascii=False, default=str)[:200_000]))
            self.db.commit()

    def drain(self, deliver, batch: int = 200, now: float | None = None) -> dict:
        """Deliver due events. `deliver(list_of_events) -> list_of_results` (one per event, in order);
        if it raises, the whole batch is retried later. Returns counts."""
        done = failed = 0
        while True:
            t = time.time() if now is None else now
            rows = self.db.execute("SELECT event_id,payload,attempts FROM events WHERE state='pending' AND next_at<=? "
                                   "ORDER BY created, rowid LIMIT ?", (t, batch)).fetchall()
            if not rows:
                break
            evs = [json.loads(r[1]) for r in rows]
            try:
                results = deliver(evs)
                if len(results) != len(evs):
                    raise RuntimeError("delivery returned the wrong number of results")
            except Exception as e:  # the store is unavailable (locked, disk full, …): back off
                err = f"{type(e).__name__}: {e}"[:500]
                with self._lock:
                    for eid, _, att in rows:
                        att += 1
                        state = "dead" if att >= MAX_ATTEMPTS else "pending"
                        self.db.execute("UPDATE events SET attempts=?, state=?, next_at=?, last_error=? WHERE event_id=?",
                                        (att, state, t + BASE_DELAY * 2 ** (att - 1), err, eid))
                    self.db.commit()
                failed += len(rows)
                break  # don't hammer a store that just failed
            with self._lock:
                for (eid, _, att), res in zip(rows, results):
                    self.db.execute("UPDATE events SET state='delivered', attempts=?, delivered=?, result=?, last_error='' "
                                    "WHERE event_id=?", (att + 1, time.time(), json.dumps(res)[:2000], eid))
                self.db.commit()
            done += len(rows)
        return {"delivered": done, "failed": failed}

    def status(self) -> dict:
        counts = {s: n for s, n in self.db.execute("SELECT state, count(*) FROM events GROUP BY state")}
        dead = [{"event_id": r[0], "attempts": r[1], "error": r[2]} for r in
                self.db.execute("SELECT event_id, attempts, last_error FROM events WHERE state='dead' ORDER BY created DESC LIMIT 20")]
        retry = [{"event_id": r[0], "attempts": r[1], "error": r[2], "next_at": r[3]} for r in
                 self.db.execute("SELECT event_id, attempts, last_error, next_at FROM events WHERE state='pending' AND attempts>0 "
                                 "ORDER BY next_at LIMIT 20")]
        invalid = self.db.execute("SELECT count(*) FROM invalid").fetchone()[0]
        return {"pending": counts.get("pending", 0), "delivered": counts.get("delivered", 0), "dead": counts.get("dead", 0),
                "invalid": invalid, "retrying": retry, "dead_letters": dead, "path": str(self.path)}

    def retry(self, dead: bool = False) -> int:
        """Make waiting events due now (and dead ones pending again, with dead=True)."""
        with self._lock:
            n = self.db.execute("UPDATE events SET next_at=0, " + ("state='pending', attempts=0 " if dead else "state=state ")
                                + "WHERE state=" + ("'dead'" if dead else "'pending'")).rowcount
            self.db.commit()
            return n

    def purge_delivered(self, older_than_days: float = 30) -> int:
        """Forget delivered events after a while (the archive holds the content; dedup uses anchors)."""
        with self._lock:
            n = self.db.execute("DELETE FROM events WHERE state='delivered' AND delivered < ?",
                                (time.time() - older_than_days * 86400,)).rowcount
            self.db.commit()
            return n

    # ── read cursors ─────────────────────────────────────────────────────
    def cursor(self, source: str) -> tuple[int, str]:
        r = self.db.execute("SELECT offset, sig FROM cursors WHERE source=?", (source,)).fetchone()
        return (r[0], r[1] or "") if r else (0, "")

    def set_cursor(self, source: str, offset: int, sig: str = "") -> None:
        with self._lock:
            self.db.execute("INSERT INTO cursors(source,offset,sig,updated) VALUES(?,?,?,?) ON CONFLICT(source) DO UPDATE "
                            "SET offset=excluded.offset, sig=excluded.sig, updated=excluded.updated", (source, offset, sig, time.time()))
            self.db.commit()
