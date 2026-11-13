"""Exports from web apps that offer no history API: ChatGPT, Claude.ai and Gemini (Google Takeout).

These are export-only. Nothing here is real time: the user downloads an export and imports the file.
ChatGPT: Settings > Data controls > Export data (zip with conversations.json).
Claude.ai: Settings > Privacy > Export data (zip with conversations.json).
Gemini web: Google Takeout > "My Activity" > Gemini Apps (MyActivity.json): activity records of
prompts (and, when present, the response as HTML). Takeout has no conversation grouping, so Cortex
groups each day's activity into one session.
"""

from __future__ import annotations

import html
import json
import re
import zipfile
from pathlib import Path

from .base import Capabilities, from_conversation

CHATGPT = Capabilities(
    provider="chatgpt", modes=("export",), realtime="no: export only (OpenAI offers no history API for ChatGPT)",
    history=True, attachments="images noted as [image]; files are not in the export text", auth="the export zip you download",
    tested="fixture", limitations=("only the branch you ended on is imported (abandoned regenerations are skipped)",))
CLAUDE_AI = Capabilities(
    provider="claude-ai", modes=("export",), realtime="no: export only", history=True, attachments="not included",
    auth="the export zip you download", tested="fixture", limitations=())
GEMINI_TAKEOUT = Capabilities(
    provider="gemini", modes=("export",), realtime="no: Google Takeout export only", history=True, attachments="not included",
    auth="the Takeout zip or MyActivity.json you download", tested="fixture",
    limitations=("partial: Takeout keeps activity records, not full threads; replies only when Takeout includes them",
                 "grouped by day, not by conversation", "format checked against samples only"))

ADAPTER = {"chatgpt": "chatgpt-export@1", "claude-ai": "claude-ai-export@1"}


def _load_takeout(path: Path):
    if path.suffix.lower() == ".zip":
        with zipfile.ZipFile(path) as z:
            name = next((n for n in z.namelist() if re.search(r"Gemini[^/]*/MyActivity\.json$", n, re.I)), None)
            if not name:
                return None
            return json.loads(z.read(name).decode("utf-8", "replace"))
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except ValueError:
        return None
    return data


def is_takeout(data) -> bool:
    return isinstance(data, list) and bool(data) and isinstance(data[0], dict) and "Gemini" in str(data[0].get("header", "")) \
        and "time" in data[0]


def takeout_events(data: list, source_uri: str = "") -> list[dict]:
    out = []
    for i, e in enumerate(data):
        if not isinstance(e, dict) or "Gemini" not in str(e.get("header", "")):
            continue
        t = str(e.get("time") or "")
        title = str(e.get("title") or "")
        prompt = re.sub(r"^(Prompted|Asked|Searched for)\s+", "", title).strip()
        if not t or not prompt:
            continue
        day = t[:10]
        session = {"id": f"takeout-{day}", "title": f"Gemini activity {day}", "started": f"{day}T00:00:00Z"}
        base = {"provider": "gemini", "adapter": "gemini-takeout@1", "capture": "export", "kind": "message",
                "session": session, "provenance": {"source_uri": source_uri}}
        out.append({**base, "message": {"id": f"{t}:prompt:{i}", "role": "user", "text": prompt, "ts": t}})
        reply = "\n".join(_html_text(str(x.get("html", ""))) for x in e.get("safeHtmlItem") or [] if isinstance(x, dict)).strip()
        if reply:
            out.append({**base, "message": {"id": f"{t}:reply:{i}", "role": "assistant", "text": reply, "ts": t}})
    return out


def _html_text(h: str) -> str:
    """Takeout's response HTML -> plain text: block tags become line breaks, inline tags vanish."""
    h = re.sub(r"(?i)<br\s*/?>|</(p|div|li|h[1-6]|tr)>", "\n", h)
    h = re.sub(r"(?i)<li[^>]*>", "- ", h)
    text = html.unescape(re.sub(r"<[^>]+>", "", h))
    return "\n".join(" ".join(line.split()) for line in text.splitlines() if line.strip())


def events_from_file(path: Path) -> list[dict]:
    """Every event in an export file (ChatGPT, Claude.ai, or Gemini Takeout). Raises ValueError if unrecognised."""
    from cortex.importers import parse_export
    data = _load_takeout(path)
    if is_takeout(data):
        return takeout_events(data, str(path))
    out = []
    for c in parse_export(path):
        provider = c.key.split(":", 1)[0]
        out += from_conversation(c, provider, ADAPTER.get(provider, provider + "-export@1"), "export", str(path))
    return out
