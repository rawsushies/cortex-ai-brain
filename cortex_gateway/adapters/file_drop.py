"""Generic intake: any tool can drop canonical events, one JSON object per line, into
<vault>/.cortex/drop/*.jsonl. Each file is processed once: moved to done/ (all valid) or failed/
(with a .errors.json report next to it). Invalid lines are kept, never silently discarded."""

from __future__ import annotations

import json
import os

from .base import Capabilities

CAPS = Capabilities(
    provider="*", modes=("file-drop",), realtime="near: on the next `cortex gateway drop` / app cycle",
    history=True, attachments="references (uri, sha256, size) only", auth="write access to the vault's .cortex/drop folder",
    tested="live", limitations=("the folder syncs with the vault, so another device may also process a file; "
                                "duplicates are removed by message anchors",))


def drop_dir(gw):
    d = gw.brain.meta_dir / "drop"
    d.mkdir(exist_ok=True)
    return d


def process(gw) -> dict:
    d = drop_dir(gw)
    files = sorted(p for p in d.glob("*.jsonl") if p.is_file())
    out = {"files": 0, "queued": 0, "invalid": 0}
    for f in files:
        raw, bad = [], []
        try:
            lines = f.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for n, line in enumerate(lines, 1):
            if not line.strip():
                continue
            try:
                o = json.loads(line)
            except ValueError as e:
                bad.append({"line": n, "reasons": [f"not JSON: {e}"]})
                continue
            if isinstance(o, dict):
                o.setdefault("capture", "file-drop")
            raw.append((n, o))
        r = gw.submit([o for _, o in raw])
        for item in r["invalid"]:
            bad.append({"line": raw[item["index"]][0], "reasons": item["reasons"]})
        dest = d / ("failed" if bad else "done")
        dest.mkdir(exist_ok=True)
        target = dest / f.name
        if target.exists():
            target = dest / f"{f.stem}-{os.urandom(3).hex()}{f.suffix}"
        os.replace(f, target)
        if bad:
            target.with_suffix(".errors.json").write_text(json.dumps(bad, indent=2), encoding="utf-8")
        out["files"] += 1
        out["queued"] += r["new"]
        out["invalid"] += len(bad)
    return out
