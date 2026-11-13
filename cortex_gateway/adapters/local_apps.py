"""Codex CLI, Gemini CLI and Cursor: the history each keeps on this computer, read-only.
Parsing reuses cortex.importers (one parser per app); this module only turns its output into events."""

from __future__ import annotations

from .base import Capabilities, from_conversation

CODEX = Capabilities(
    provider="codex", modes=("local-file",), realtime="no: on scan (the app imports every few minutes)", history=True,
    attachments="none", auth="read access to ~/.codex/sessions (or CODEX_HOME)", tested="fixture",
    limitations=("format documented by the Codex CLI; checked against sample files, not a live install",
                 "a reply still being written is stored again as a revision once it grows"))
GEMINI_CLI = Capabilities(
    provider="gemini-cli", modes=("local-file",), realtime="no: on scan", history=True, attachments="none",
    auth="read access to ~/.gemini/tmp", tested="fixture",
    limitations=("older Gemini CLI versions log prompts only (no replies)",
                 "checked against sample files, not a live install"))
CURSOR = Capabilities(
    provider="cursor", modes=("local-file",), realtime="no: on scan", history=True, attachments="none",
    auth="read access to Cursor's state.vscdb", tested="fixture",
    limitations=("experimental: Cursor changes its storage format often", "messages carry no timestamps"))

SOURCES = {"codex": ("codex", "codex-jsonl@1"), "gemini": ("gemini-cli", "gemini-cli@1"), "cursor": ("cursor", "cursor-vscdb@1")}


def events_for(conv, source_uri: str = "") -> list[dict]:
    prefix = conv.key.split(":", 1)[0]
    provider, adapter = SOURCES[prefix]
    return from_conversation(conv, provider, adapter, "local-file", source_uri)
