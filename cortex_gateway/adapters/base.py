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
