"""`cortex gateway …`: capture conversations from providers into the archive.

    cortex gateway capabilities              what each adapter can and can't do
    cortex gateway scan [--provider claude-code]   read new conversation lines (needs history import on)
    cortex gateway hook claude-code          for Claude Code's Stop/SessionEnd hook (reads JSON on stdin)
    cortex gateway drop                      process files in <vault>/.cortex/drop
    cortex gateway status                    queue: pending, retrying, dead letters, invalid input
    cortex gateway retry [--dead]            try waiting (or dead) events again now
    cortex gateway import FILE               a ChatGPT / Claude.ai / Gemini Takeout export
    cortex gateway forget [PROVIDER] [--session ID] [--before DATE] [--dry-run]   delete for good
    cortex gateway retain                    forget conversations older than each provider's retention
    cortex gateway token [--new]             the bearer token for POST /api/events (local apps, webhooks)
"""

from __future__ import annotations

import json
import sys


def token(new: bool = False) -> str:
    """The REST intake token, kept in this machine's Cortex config (never in the synced vault)."""
    import secrets
    from cortex.cli import load_config, save_config
    cfg = load_config()
    if new or not cfg.get("gateway_token"):
        cfg["gateway_token"] = secrets.token_urlsafe(32)
        save_config(cfg)
    return cfg["gateway_token"]


def retain(brain) -> dict:
    """Apply each provider's retention_days from .cortex/capture.json."""
    from datetime import datetime, timedelta
    from cortex.archive import forget
    from . import rules as R
    out = {}
    for prov, cfg in R.load(brain.root)["providers"].items():
        days = cfg.get("retention_days")
        if days:
            cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S")
            out[prov] = forget(brain, provider=prov, before=cutoff, agent="cortex")["count"]
    return {"forgot": out}


def run(vault, action: str, provider: str | None = None, dead: bool = False, stdin=None, session: str | None = None,
        before: str | None = None, dry_run: bool = False, file: str | None = None, new: bool = False) -> dict:
    from cortex.core import Brain
    from . import Gateway
    from .adapters import ADAPTERS, capabilities, claude_code, file_drop
    if action == "capabilities":
        return {"adapters": capabilities()}
    if action == "token":
        return {"token": token(new), "endpoint": "POST http://127.0.0.1:7777/api/events (Authorization: Bearer <token>)"}
    b = Brain(vault)
    gw = Gateway(b)
    try:
        if action in ("scan", "hook"):
            from cortex.cli import import_allowed
            if not import_allowed():  # the same consent as the importer: nothing is read until the user says yes
                return {"skipped": "history capture is off: turn on 'Import my AI history' in the app, or run `cortex import` once"}
        if action == "scan":
            prov = provider or "claude-code"
            if prov not in ADAPTERS or prov == "file-drop":
                raise ValueError(f"no scan adapter for {prov!r} (available: claude-code)")
            r = {"scan": claude_code.scan(gw)}
        elif action == "hook":
            if (provider or "claude-code") != "claude-code":
                raise ValueError("hooks are supported for claude-code only")
            payload = json.loads((stdin or sys.stdin).read() or "{}")
            r = {"hook": claude_code.hook(gw, payload)}
        elif action == "drop":
            r = {"drop": file_drop.process(gw)}
        elif action == "retry":
            r = {"retried": gw.queue.retry(dead=dead)}
        elif action == "status":
            return gw.queue.status()
        elif action == "import":
            from pathlib import Path
            from .adapters import exports
            if not file:
                raise ValueError("give the export file: cortex gateway import <file>")
            sub = gw.submit(exports.events_from_file(Path(file).expanduser()))
            r = {"import": {"queued": sub["new"], "skipped": len(sub["skipped"]), "invalid": len(sub["invalid"])}}
        elif action == "forget":
            from cortex.archive import forget
            r = {"forget": forget(b, provider=provider, session=session, before=before, dry_run=dry_run)}
            if not dry_run:
                n = purge_queue(gw, provider, session)
                r["forget"]["queue_rows_removed"] = n
            return r
        elif action == "retain":
            return retain(b)
        else:
            raise ValueError(f"unknown action {action!r}")
        r["delivery"] = gw.run()
        r["queue"] = {k: v for k, v in gw.queue.status().items() if k in ("pending", "delivered", "dead", "invalid")}
        return r
    finally:
        gw.close()
        b.close()


def purge_queue(gw, provider: str | None, session: str | None) -> int:
    """Drop queued copies (they hold message text) of what was just forgotten."""
    import json as _json
    rows = gw.queue.db.execute("SELECT event_id, payload FROM events").fetchall()
    gone = [eid for eid, pl in rows if (lambda e: (not provider or e.get("provider") == provider)
                                        and (not session or e.get("session", {}).get("id") == session))(_json.loads(pl))]
    if provider or session:
        gw.queue.db.executemany("DELETE FROM events WHERE event_id=?", [(g,) for g in gone])
        gw.queue.db.commit()
    return len(gone) if (provider or session) else 0
