"""Cortex Gateway: provider-independent memory ingestion.

Adapters (cortex_gateway.adapters) turn what a provider offers (local transcripts, hooks, exports,
MCP calls, dropped files) into canonical events (schema.py). The gateway validates and normalizes
them, keeps them in a persistent retryable queue (queue.py), and delivers them to Cortex's
append-only conversation archive (cortex/archive.py). Provider-specific code lives here, never in
Cortex's core. No LLM is involved anywhere.
"""

from __future__ import annotations

import json

from .queue import Queue
from .schema import normalize, validate

__all__ = ["Gateway"]


class Gateway:
    def __init__(self, brain, agent: str = "cortex-gateway"):
        from cortex.archive import Archive
        self.brain = brain
        self.agent = agent
        self.queue = Queue(brain.local_dir / "gateway.db")
        self.archive = Archive(brain)

    def close(self) -> None:
        self.queue.close()

    def submit(self, raw_events: list) -> dict:
        """Normalize and validate; queue the valid ones (idempotent), keep the invalid ones with reasons."""
        good, invalid = [], []
        for i, raw in enumerate(raw_events if isinstance(raw_events, list) else []):
            ev = normalize(raw)
            errs = validate(ev)
            if errs:
                invalid.append({"index": i, "reasons": errs})
                self.queue.reject(raw, errs)
            else:
                good.append(ev)
        new = self.queue.enqueue(good) if good else 0
        return {"accepted": len(good), "new": new, "invalid": invalid, "events": good}

    def deliver(self, events: list[dict]) -> list[dict]:
        return self.archive.append(events, agent=self.agent)

    def run(self) -> dict:
        """Deliver everything that's due; then group new conversations into topics (bounded)."""
        r = self.queue.drain(self.deliver)
        if r["delivered"]:
            try:
                from cortex.organize import Organizer
                Organizer(self.brain).group_conversations(agent="cortex", limit=200)
            except Exception:
                pass
        return r

    def ingest_now(self, raw_events: list) -> list[dict]:
        """For MCP/REST callers: queue + deliver immediately, and say what happened to each event:
        archived | duplicate | invalid | queued (store busy; it will be retried)."""
        if not isinstance(raw_events, list) or not raw_events:
            raise ValueError("events must be a non-empty list")
        if len(raw_events) > 500:
            raise ValueError("at most 500 events per call")
        known = set()
        sub = self.submit(raw_events)
        ids = [e["event_id"] for e in sub["events"]]
        if ids:
            q = ",".join("?" * len(ids))
            known = {r[0] for r in self.queue.db.execute(
                f"SELECT event_id FROM events WHERE event_id IN ({q}) AND state='delivered'", ids)}
        self.queue.drain(self.deliver)
        out, good, seen = [], iter(sub["events"]), set()
        bad = {x["index"]: x["reasons"] for x in sub["invalid"]}
        for i in range(len(raw_events)):
            if i in bad:
                out.append({"index": i, "status": "invalid", "reasons": bad[i]})
                continue
            ev = next(good)
            if ev["event_id"] in seen:  # the same event twice in one call
                out.append({"index": i, "event_id": ev["event_id"], "status": "duplicate"})
                continue
            seen.add(ev["event_id"])
            row = self.queue.db.execute("SELECT state, result, last_error FROM events WHERE event_id=?", (ev["event_id"],)).fetchone()
            if ev["event_id"] in known:
                st = {"status": "duplicate"}
            elif row and row[0] == "delivered":
                st = json.loads(row[1] or "{}")
            else:
                st = {"status": "queued", "error": row[2] if row else ""}
            out.append({"index": i, "event_id": ev["event_id"], **{k: v for k, v in st.items() if k != "event_id"}})
        return out
