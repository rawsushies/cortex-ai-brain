"""What every adapter declares about itself, so nobody has to guess what a provider supports."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field


@dataclass(frozen=True)
class Capabilities:
    provider: str
    modes: tuple            # how capture happens: local-file, hook, export, mcp, rest, file-drop
    realtime: str           # "no", "near (…)", "yes (…)": stated plainly, never implied
    history: bool           # can it import past conversations?
    attachments: str        # what it knows about attachments
    auth: str               # what access it needs
    tested: str             # "live" (verified against real data) or "fixture" (documented format only)
    limitations: tuple = field(default_factory=tuple)

    def to_dict(self) -> dict:
        return asdict(self)


def from_conversation(c, provider: str, adapter: str, capture: str, source_uri: str = "") -> list[dict]:
    """Raw canonical events from a parsed importer Conversation. Message ids are positions in the
    conversation (stable for append-only logs and for exports, which replay from the start). If a
    provider regenerates a reply, that position's content changes and the archive appends a revision."""
    sid = c.key.split(":", 1)[1] if ":" in c.key else c.key
    session = {"id": sid, "title": c.title, "project": c.project, "started": c.started, "updated": c.updated}
    out = []
    for i, m in enumerate(c.messages):
        out.append({"provider": provider, "adapter": adapter, "capture": capture, "kind": "message",
                    "session": session, "message": {"id": f"seq:{i}", "role": m.role, "text": m.text, "ts": m.ts},
                    "provenance": {"source_uri": source_uri}})
    if c.title:
        out.append({"provider": provider, "adapter": adapter, "capture": capture, "kind": "session",
                    "session": session, "message": {"id": "session"}, "provenance": {"source_uri": source_uri}})
    return out
