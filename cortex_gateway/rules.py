"""Capture rules (.cortex/capture.json, synced with the vault, editable in the app's Settings):

    {"providers": {"chatgpt": {"enabled": false}, "claude-code": {"retention_days": 90}},
     "projects": {"exclude": ["*client-secret*"], "include": []},
     "redact": ["INTERNAL-[0-9]{6}"]}

A disabled provider or an excluded project is never stored (events come back as `skipped`).
`redact` adds patterns on top of the built-in credential filter. Retention is enforced by
`cortex gateway retain` (and daily by the app), which forgets older conversations for good."""

from __future__ import annotations

import json
import re
from pathlib import Path

DEFAULT = {"providers": {}, "projects": {"exclude": [], "include": []}, "redact": []}


def path(vault: Path) -> Path:
    return vault / ".cortex" / "capture.json"


def load(vault: Path) -> dict:
    try:
        data = json.loads(path(vault).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return json.loads(json.dumps(DEFAULT))
    return clean(data) if isinstance(data, dict) else json.loads(json.dumps(DEFAULT))


def clean(data: dict) -> dict:
    """Validate a rules object (raises ValueError with a readable message)."""
    provs = data.get("providers", {})
    if provs is None:
        provs = {}
    if not isinstance(provs, dict):
        raise ValueError("providers must be an object")
    out_p = {}
    for name, cfg in list(provs.items())[:50]:
        if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,39}", str(name)) or not isinstance(cfg, dict):
            raise ValueError(f"bad provider entry {name!r}")
        days = cfg.get("retention_days")
        if days not in (None, "", 0) and (not isinstance(days, int) or isinstance(days, bool) or days < 1 or days > 36500):
            raise ValueError(f"retention_days for {name} must be a whole number of days (1-36500) or empty")
        out_p[name] = {"enabled": cfg.get("enabled", True) is not False, **({"retention_days": days} if days else {})}
    proj = data.get("projects", {}) if data.get("projects") is not None else {}
    if not isinstance(proj, dict):
        raise ValueError("projects must be an object")
    globs = {k: [str(x)[:200] for x in (proj.get(k) or []) if str(x).strip()][:100] for k in ("exclude", "include")}
    red = []
    for pat in (data.get("redact") or [])[:50]:
        pat = str(pat)[:300]
        try:
            re.compile(pat)
        except re.error as e:
            raise ValueError(f"bad redact pattern {pat!r}: {e}")
        red.append(pat)
    return {"providers": out_p, "projects": globs, "redact": red}


def save(vault: Path, data: dict) -> dict:
    import os
    out = clean(data)
    f = path(vault)
    f.parent.mkdir(exist_ok=True)
    tmp = f.with_suffix(".tmp")
    tmp.write_text(json.dumps(out, indent=2), encoding="utf-8")
    os.replace(tmp, f)
    return out
