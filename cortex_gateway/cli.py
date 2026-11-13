"""`cortex gateway …`: capture conversations from providers into the archive.

    cortex gateway capabilities              what each adapter can and can't do
    cortex gateway scan [--provider claude-code]   read new conversation lines (needs history import on)
    cortex gateway hook claude-code          for Claude Code's Stop/SessionEnd hook (reads JSON on stdin)
    cortex gateway drop                      process files in <vault>/.cortex/drop
    cortex gateway status                    queue: pending, retrying, dead letters, invalid input
    cortex gateway retry [--dead]            try waiting (or dead) events again now
"""

from __future__ import annotations

import json
import sys


def run(vault, action: str, provider: str | None = None, dead: bool = False, stdin=None) -> dict:
    from cortex.core import Brain
    from . import Gateway
    from .adapters import ADAPTERS, capabilities, claude_code, file_drop
    if action == "capabilities":
        return {"adapters": capabilities()}
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
        else:
            raise ValueError(f"unknown action {action!r}")
        r["delivery"] = gw.run()
        r["queue"] = {k: v for k, v in gw.queue.status().items() if k in ("pending", "delivered", "dead", "invalid")}
        return r
    finally:
        gw.close()
        b.close()
