"""Claude Code: the session transcripts it keeps on disk (~/.claude/projects/*/*.jsonl), read-only.

Two ways in, both supported by Claude Code itself:
- scan: read every transcript (only the bytes added since the last scan).
- hook: Claude Code runs a command on Stop / SessionEnd / PreCompact and passes JSON on stdin with
  `transcript_path`; `cortex gateway hook claude-code` reads just that transcript's new lines.

Each transcript line has its own `uuid`, which becomes the message id, so a message keeps the same
identity on every device and every re-read.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from .base import Capabilities

PROVIDER = "claude-code"
ADAPTER = "claude-code-jsonl@1"
CAPS = Capabilities(
    provider=PROVIDER, modes=("local-file", "hook"),
    realtime="near: after each reply when the Stop hook is enabled; otherwise on scan",
    history=True, attachments="images are noted as [image]; files are not copied",
    auth="read access to ~/.claude/projects (or CLAUDE_CONFIG_DIR)", tested="live",
    limitations=("tool output and hidden reasoning are summarised or dropped, like the importer",
                 "sub-agent (sidechain) turns are skipped",
                 "transcripts Claude Code has already cleaned up can't be recovered"))


def projects_dir() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude")) / "projects"


def transcripts() -> list[Path]:
    d = projects_dir()
    return sorted(d.glob("*/*.jsonl")) if d.is_dir() else []


def _text(o: dict) -> str:
    from cortex.importers import _clean_user, _tool_line
    content = (o.get("message") or {}).get("content")
    if o.get("type") == "user":
        if isinstance(content, str):
            return _clean_user(content)
        parts = []
        for b in content if isinstance(content, list) else []:
            if isinstance(b, dict) and b.get("type") == "text":
                parts.append(_clean_user(b.get("text", "")))
            elif isinstance(b, dict) and b.get("type") == "image":
                parts.append("[image]")
        return "\n".join(x for x in parts if x).strip()
    parts = []
    for b in content if isinstance(content, list) else [{"type": "text", "text": content or ""}]:
        if not isinstance(b, dict):
            continue
        if b.get("type") == "text":
            parts.append(b.get("text", ""))
        elif b.get("type") == "tool_use":
            parts.append(_tool_line(b.get("name", "tool"), b.get("input")))
    return "\n".join(x for x in parts if x).strip()


def read_new(path: Path, offset: int) -> tuple[list[dict], int]:
    """Raw events from the complete lines after `offset`, and the offset to resume from.
    A half-written last line is left for next time; a truncated/replaced file starts over
    (the archive's anchors make re-reading harmless)."""
    size = path.stat().st_size
    if offset > size:
        offset = 0
    events, title = [], ""
    with open(path, "rb") as f:
        f.seek(offset)
        data = f.read()
    end = data.rfind(b"\n") + 1
    pos = offset
    for raw in data[:end].split(b"\n")[:-1] if end else []:
        line_at, pos = pos, pos + len(raw) + 1
        try:
            o = json.loads(raw.decode("utf-8", "replace"))
        except ValueError:
            continue
        if not isinstance(o, dict):
            continue
        sid = str(o.get("sessionId") or path.stem)
        if o.get("type") == "summary" and o.get("summary"):
            title = str(o["summary"])
            events.append({"provider": PROVIDER, "adapter": ADAPTER, "kind": "session",
                           "session": {"id": sid, "title": title},
                           "message": {"id": "session"}, "provenance": {"source_uri": str(path), "source_offset": line_at}})
            continue
        if o.get("type") not in ("user", "assistant") or o.get("isSidechain") or o.get("isMeta") or not o.get("uuid"):
            continue
        text = _text(o)
        if not text:
            continue  # tool results and empty turns
        events.append({"provider": PROVIDER, "adapter": ADAPTER, "kind": "message",
                       "session": {"id": sid, "title": title, "project": o.get("cwd") or "", "started": o.get("timestamp")},
                       "message": {"id": str(o["uuid"]), "role": o["type"], "text": text, "ts": o.get("timestamp"),
                                   "model": (o.get("message") or {}).get("model") or ""},
                       "provenance": {"source_uri": str(path), "source_offset": line_at}})
    return events, offset + end


def scan(gw, capture: str = "local-file", paths: list[Path] | None = None) -> dict:
    """Queue what's new in each transcript; the cursor moves only after the events are safely queued."""
    files = paths if paths is not None else transcripts()
    total = new = 0
    for p in files:
        try:
            st = p.stat()
        except OSError:
            continue
        src = f"{PROVIDER}:{p}"
        offset, sig = gw.queue.cursor(src)
        cur_sig = f"{st.st_size}:{st.st_mtime_ns}"
        if sig == cur_sig:
            continue
        try:
            raw, nxt = read_new(p, offset)
        except OSError:
            continue
        for e in raw:
            e["capture"] = capture
        r = gw.submit(raw)
        gw.queue.set_cursor(src, nxt, cur_sig)
        total += len(raw)
        new += r["new"]
    return {"files": len(files), "read": total, "queued": new}


def hook(gw, payload: dict) -> dict:
    """Claude Code hook input -> queue that transcript's new lines. Only transcripts inside Claude
    Code's own projects folder are read (the hook input can't point Cortex at arbitrary files)."""
    tp = payload.get("transcript_path") if isinstance(payload, dict) else None
    if not tp:
        raise ValueError("hook input has no transcript_path")
    p = Path(str(tp)).expanduser().resolve()
    base = projects_dir().resolve()
    if p.suffix != ".jsonl" or base not in p.parents or not p.is_file():
        raise ValueError("transcript_path must be a .jsonl file inside Claude Code's projects folder")
    return scan(gw, capture="hook", paths=[p])
