"""The canonical memory event (cortex.event/1): one message, session record or agent event, from any
provider, in one shape. Adapters produce these; Cortex only ever receives these.

Identity is deterministic, so the same message captured twice (two devices, a re-scan, a retry) has
the same ids and is stored once:

    message_key = sha256(provider | session.id | message.id)[:12]     which message
    content_hash = sha256(normalized text)[:8]                         which version of it
    event_id    = sha256(provider | session.id | message.id | content_hash)

normalize() fills sensible defaults for partial metadata and never raises; validate() returns a
list of problems (empty = valid). Text is redacted (credentials) before it can reach disk.
"""

from __future__ import annotations

import hashlib
import re
import socket
from datetime import datetime, timezone

SCHEMA = "cortex.event/1"
KINDS = ("message", "session", "agent_event")
ROLES = ("user", "assistant", "tool", "system")
CAPTURES = ("local-file", "hook", "export", "mcp", "rest", "file-drop")
MAX_TEXT = 400_000
PROVIDER_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,39}$")
DISPLAY = {"claude-code": "Claude Code", "codex": "Codex", "gemini-cli": "Gemini CLI", "cursor": "Cursor",
           "chatgpt": "ChatGPT", "claude-ai": "Claude.ai", "gemini": "Gemini", "core": "COre"}
INVISIBLE = re.compile("[​‌‎‏‪-‮⁠-⁤⁦-⁩﻿\U000e0000-\U000e007f]")


def _h(*parts: str) -> str:
    return hashlib.sha256("\x1f".join(parts).encode("utf-8", "replace")).hexdigest()


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def iso(v) -> str:
    if v in (None, ""):
        return ""
    if isinstance(v, (int, float)):
        v = v / 1000 if v > 1e12 else v
        return datetime.fromtimestamp(v, timezone.utc).astimezone().isoformat(timespec="seconds")
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).astimezone().isoformat(timespec="seconds")
    except ValueError:
        return ""


def display_name(provider: str) -> str:
    return DISPLAY.get(provider, provider.replace("-", " ").title())


def message_key(provider: str, session_id: str, message_id: str) -> str:
    return _h(provider, session_id, message_id)[:12]


def content_hash(text: str) -> str:
    return _h(" ".join(text.split()))[:8]


def _str(v, n: int = 300) -> str:
    return "" if v is None else str(v)[:n]


def normalize(raw: dict, device: str | None = None) -> dict:
    """Coerce anything event-shaped into the canonical form. Never raises on bad input; problems
    are left for validate() to report (so nothing is silently dropped)."""
    from cortex.importers import redact
    r = raw if isinstance(raw, dict) else {}
    s = r.get("session") if isinstance(r.get("session"), dict) else {}
    m = r.get("message") if isinstance(r.get("message"), dict) else {}
    p = r.get("provenance") if isinstance(r.get("provenance"), dict) else {}
    provider = _str(r.get("provider"), 40).strip().lower()
    text = _str(m.get("text"), MAX_TEXT + 1)
    visible = INVISIBLE.sub("", text)
    clean = redact(visible)
    redactions = clean != visible
    session = {"id": _str(s.get("id"), 200).strip(), "title": " ".join(_str(s.get("title"), 200).split()),
               "project": _str(s.get("project"), 300), "started": iso(s.get("started")), "updated": iso(s.get("updated"))}
    msg_id = _str(m.get("id"), 200).strip()
    kind = _str(r.get("kind") or "message", 20)
    seq = m.get("seq")
    if not msg_id and isinstance(seq, int):
        msg_id = f"seq:{seq}"
    if not msg_id and kind == "message" and clean:
        msg_id = "h:" + content_hash(clean)  # last resort: identity by content
    if kind == "session":
        msg_id = msg_id or "session"
    ev = {
        "schema": SCHEMA, "provider": provider, "adapter": _str(r.get("adapter") or "unknown", 60),
        "capture": _str(r.get("capture") or "mcp", 20), "kind": kind, "session": session,
        "message": {"id": msg_id, "role": _str(m.get("role") or "user", 20).lower(), "text": clean,
                    "ts": iso(m.get("ts")), "model": _str(m.get("model"), 80)},
        "attachments": [a for a in (_attachment(x) for x in (r.get("attachments") or []) if isinstance(r.get("attachments"), list)) if a][:50],
        "provenance": {"device": _str(p.get("device") or device or socket.gethostname(), 60),
                       "captured_at": iso(p.get("captured_at")) or now_iso(),
                       "source_uri": _str(p.get("source_uri"), 500), "source_offset": p.get("source_offset")
                       if isinstance(p.get("source_offset"), int) else None},
        "privacy": {"redacted": bool(redactions)},
    }
    if not session["started"]:
        session["started"] = ev["message"]["ts"] or ev["provenance"]["captured_at"]
    ev["message_key"] = message_key(provider, session["id"], msg_id) if provider and session["id"] and msg_id else ""
    ev["content_hash"] = content_hash(clean + "".join(a["sha256"] or a["name"] for a in ev["attachments"]))
    ev["event_id"] = _h(provider, session["id"], msg_id, ev["content_hash"]) if ev["message_key"] else ""
    return ev


def _attachment(a) -> dict | None:
    """A reference to a file, never its bytes: name, type, size, hash and where it lives."""
    if not isinstance(a, dict):
        return None
    uri = _str(a.get("uri"), 500)
    if uri and not re.match(r"^(https?://|file://|/|[A-Za-z]:[\\/])", uri):
        uri = ""  # only plain locations; no javascript:, data: etc.
    sha = _str(a.get("sha256"), 64).lower()
    return {"name": " ".join(_str(a.get("name") or "attachment", 200).split()), "mime": _str(a.get("mime"), 80),
            "size": a.get("size") if isinstance(a.get("size"), int) and a.get("size") >= 0 else None,
            "sha256": sha if re.fullmatch(r"[0-9a-f]{64}", sha) else "", "uri": uri}


def validate(ev: dict) -> list[str]:
    """Problems with a normalized event; an empty list means it can be stored."""
    errs = []
    if not PROVIDER_RE.match(ev.get("provider", "")):
        errs.append("provider must be a short lowercase name like 'claude-code'")
    if ev.get("kind") not in KINDS:
        errs.append(f"kind must be one of {', '.join(KINDS)}")
    if ev.get("capture") not in CAPTURES:
        errs.append(f"capture must be one of {', '.join(CAPTURES)}")
    if not ev.get("session", {}).get("id"):
        errs.append("session.id is required (the provider's conversation id)")
    msg = ev.get("message", {})
    if ev.get("kind") != "session":
        if msg.get("role") not in ROLES:
            errs.append(f"message.role must be one of {', '.join(ROLES)}")
        if not msg.get("text") and not ev.get("attachments"):
            errs.append("message.text is empty")
        if len(msg.get("text", "")) > MAX_TEXT:
            errs.append(f"message.text is longer than {MAX_TEXT:,} characters")
    if not ev.get("event_id"):
        errs.append("could not derive an event id (needs provider, session.id and message.id or text)")
    return errs
