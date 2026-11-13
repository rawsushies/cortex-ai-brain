"""Optional: tell a local app (e.g. COre's notify endpoint) about changes when it isn't connected
over MCP. Off unless CORTEX_NOTIFY_URL is set; only loopback addresses are allowed, so vault
activity can never be sent off the machine.

    CORTEX_NOTIFY_URL=http://127.0.0.1:47321/cortex   CORTEX_NOTIFY_TOKEN=<token>

Each change is POSTed as JSON: {"source": "cortex", "type": "changed", "paths": [...], "actor": ...}
with "Authorization: Bearer <token>". Failures are ignored (the app may simply be closed)."""

from __future__ import annotations

import json
import os
import threading
import urllib.request
from urllib.parse import urlparse

LOOPBACK = {"127.0.0.1", "localhost", "::1"}


def notify_url() -> str | None:
    url = os.environ.get("CORTEX_NOTIFY_URL", "").strip()
    if not url:
        return None
    u = urlparse(url)
    if u.scheme != "http" or (u.hostname or "") not in LOOPBACK:
        return None  # never off-machine
    return url


def attach(brain) -> bool:
    url = notify_url()
    if not url:
        return False
    token = os.environ.get("CORTEX_NOTIFY_TOKEN", "")

    def post(ev: dict) -> None:
        body = json.dumps({"source": "cortex", **{k: v for k, v in ev.items() if k != "removed"}}).encode()
        req = urllib.request.Request(url, data=body, method="POST", headers={
            "Content-Type": "application/json", **({"Authorization": f"Bearer {token}"} if token else {})})
        try:
            urllib.request.urlopen(req, timeout=3).close()
        except Exception:
            pass

    brain.listeners.append(lambda ev: threading.Thread(target=post, args=(ev,), daemon=True).start())
    return True
