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
        """Normalize and validate; apply capture rules and tombstones; queue the valid ones (idempotent),
        keep the invalid ones with reasons. Skipped events are not stored anywhere."""
        import re
        from cortex.archive import project_allowed
        from . import rules as R
        rules = R.load(self.brain.root)
        extra = [re.compile(p) for p in rules["redact"]]
        good, invalid, skipped = [], [], []
        for i, raw in enumerate(raw_events if isinstance(raw_events, list) else []):
            ev = normalize(raw)
            errs = validate(ev)
            if errs:
                invalid.append({"index": i, "reasons": errs})
                self.queue.reject(raw, errs)
                continue
            why = None
            if not rules["providers"].get(ev["provider"], {}).get("enabled", True):
                why = f"capture from {ev['provider']} is turned off"
            elif not project_allowed(rules["projects"], ev["session"]["project"]):
                why = "this project is excluded from capture"
            elif self.archive.tombstones.blocks(ev["provider"], ev["session"]["id"], ev["session"]["started"] or ev["message"]["ts"]):
                why = "forgotten"
            if why:
                skipped.append({"index": i, "reason": why, "event_id": ev["event_id"]})
                continue
            if extra and ev["message"]["text"]:
                text = ev["message"]["text"]
                for rx in extra:
                    text = rx.sub("[REDACTED]", text)
                if text != ev["message"]["text"]:
                    ev["message"]["text"] = text
                    ev["privacy"]["redacted"] = True
            good.append(ev)
        new = self.queue.enqueue(good) if good else 0
        return {"accepted": len(good), "new": new, "invalid": invalid, "skipped": skipped, "events": good}

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
        skip = {x["index"]: x for x in sub["skipped"]}
        for i in range(len(raw_events)):
            if i in bad:
                out.append({"index": i, "status": "invalid", "reasons": bad[i]})
                continue
            if i in skip:
                out.append({"index": i, "event_id": skip[i]["event_id"], "status": "skipped", "reason": skip[i]["reason"]})
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
