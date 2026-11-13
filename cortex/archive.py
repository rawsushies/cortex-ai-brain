"""The conversation archive: what was actually said, kept as it was said. Append-only.

Layer A of the memory (layer B, knowledge, is organize.py). Canonical events (cortex_gateway.schema)
arrive here and become blocks in one Markdown note per conversation:

    Conversations/<Provider>/<YYYY-MM>/<date> <time> <title> (<id>).md

    **You** · 2026-11-13 10:22 <!-- m:3f9a0c1b2d4e h:9c1e22aa -->

    the message, as captured (credentials redacted)

Rules:
- Existing blocks are never rewritten. A message seen again with the same content is a duplicate; with
  different content (the provider edited or regenerated it) it is appended again, marked "revised".
- The anchor comment (hidden in Obsidian's reading view) makes dedup deterministic on every device:
  whichever machine captured a message, the same message has the same anchor.
- The frontmatter is written once, when the note is created. "Last updated" is the file's own time.
- Files written by the older importer (no anchors) are adopted, not rewritten: a message already in
  the file (by its text) counts as a duplicate, new ones are appended with anchors.
"""

from __future__ import annotations

import hashlib
import os
import re
import time
from contextlib import contextmanager
from pathlib import Path

ANCHOR = re.compile(r"<!-- m:([0-9a-f]{12}) h:([0-9a-f]{8}) -->")
AI_NAMES = {"Claude Code": "Claude", "Claude.ai": "Claude", "Codex": "Codex", "Gemini CLI": "Gemini",
            "Gemini": "Gemini", "Cursor": "Cursor", "ChatGPT": "ChatGPT", "COre": "COre"}


def _norm(s: str) -> str:
    return " ".join(s.split()).lower()


def legacy_ident(provider: str, session_id: str) -> str:
    """The id suffix the pre-gateway importer used in file names (so its files are found and adopted)."""
    return re.sub(r"[^A-Za-z0-9]", "", session_id)[-6:] or "x"


class Archive:
    def __init__(self, brain):
        self.brain = brain
        self._where: dict[tuple, str] = {}

    # ── locating a conversation's note ───────────────────────────────────
    def _ident(self, session_id: str) -> str:
        return hashlib.sha256(session_id.encode("utf-8", "replace")).hexdigest()[:8]

    def find(self, provider: str, session_id: str) -> str | None:
        key = (provider, session_id)
        if key in self._where and (self.brain.root / self._where[key]).exists():
            return self._where[key]
        from cortex_gateway.schema import display_name
        base = self.brain.root / "Conversations" / display_name(provider)
        if base.is_dir():
            for ident in (self._ident(session_id), legacy_ident(provider, session_id)):
                hits = sorted(base.glob(f"*/* ({ident}).md"))
                for h in hits:
                    if ident != self._ident(session_id) and f"session: {session_id}" not in h.read_text(
                            encoding="utf-8", errors="replace")[:2000]:
                        continue  # a 6-character legacy id can collide: confirm by the session line
                    rel = h.relative_to(self.brain.root).as_posix()
                    self._where[key] = rel
                    return rel
        return None

    def _new_path(self, ev: dict) -> str:
        from cortex.importers import _safe
        from cortex_gateway.schema import display_name
        s = ev["session"]
        started = s["started"] or ev["provenance"]["captured_at"]
        day, hm = started[:10], started[11:16].replace(":", "")
        title = s["title"] or _first_line(ev["message"]["text"]) or "Untitled conversation"
        return (f"Conversations/{_safe(display_name(ev['provider']), 30)}/{day[:7]}/"
                f"{day} {hm} {_safe(title)} ({self._ident(s['id'])}).md")

    @contextmanager
    def _lock(self, rel: str):
        """One writer per conversation note across processes on this machine (other devices are
        handled by the sync tool's conflict copies, which health reports)."""
        d = self.brain.local_dir / "locks"
        d.mkdir(exist_ok=True)
        f = d / (hashlib.sha256(rel.encode()).hexdigest()[:16] + ".lock")
        deadline = time.monotonic() + 30
        while True:
            try:
                fd = os.open(f, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.close(fd)
                break
            except FileExistsError:
                try:
                    if time.time() - f.stat().st_mtime > 60:  # left behind by a crashed process
                        f.unlink()
                        continue
                except OSError:
                    continue
                if time.monotonic() > deadline:
                    raise TimeoutError(f"{rel} is busy (another Cortex process is writing it)")
                time.sleep(0.02)
        try:
            yield
        finally:
            try:
                f.unlink()
            except OSError:
                pass

    # ── writing ──────────────────────────────────────────────────────────
    def append(self, events: list[dict], agent: str = "cortex") -> list[dict]:
        """Store normalized, validated events. Returns one result per event, in order:
        {event_id, status: archived|duplicate, path, revision?}."""
        groups: dict[tuple, list[int]] = {}
        for i, ev in enumerate(events):
            groups.setdefault((ev["provider"], ev["session"]["id"]), []).append(i)
        results: list[dict] = [{}] * len(events)
        touched = []
        for (provider, sid), idxs in groups.items():
            evs = [events[i] for i in idxs]
            first = next((e for e in evs if e["kind"] == "message"), evs[0])
            rel = self.find(provider, sid) or self._new_path(first)
            with self._lock(rel):
                out = self._append_session(rel, provider, sid, evs, agent)
            for i, r in zip(idxs, out):
                results[i] = r
            if any(r["status"] == "archived" for r in out):
                touched.append(rel)
        return results

    def _append_session(self, rel: str, provider: str, sid: str, evs: list[dict], agent: str) -> list[dict]:
        from cortex_gateway.schema import display_name
        b = self.brain
        p = b.resolve(rel)
        created = False
        if not p.exists():
            s = next((e["session"] for e in evs if e["session"].get("title")), evs[0]["session"])
            title = s["title"] or _first_line(next((e["message"]["text"] for e in evs if e["kind"] == "message"), "")) or "Untitled conversation"
            fm = ["---", f"tags: [conversation, {re.sub(r'[^a-z0-9]+', '-', provider).strip('-')}]",
                  f"source: {display_name(provider)}", f"provider: {provider}", f"session: {sid}"]
            if s.get("project"):
                fm.append(f"project: {_yaml(s['project'])}")
            fm += [f"started: {s['started']}", f"captured_by: cortex-gateway ({evs[0]['adapter']})", "---",
                   f"# {' '.join(title.split())[:120]}", ""]
            b.store(rel, "\n".join(fm))
            self._where[(provider, sid)] = rel
            created = True
        text = p.read_text(encoding="utf-8", errors="replace")
        seen = set(ANCHOR.findall(text))
        keys = {k for k, _ in seen}
        legacy = not seen and not created
        body_norm = _norm(text) if legacy else ""
        blocks, out = [], []
        order = sorted(range(len(evs)), key=lambda i: (evs[i]["message"]["ts"] or "", i))
        res: dict[int, dict] = {}
        for i in order:
            ev = evs[i]
            pair = (ev["message_key"], ev["content_hash"])
            if ev["kind"] == "session":
                res[i] = {"event_id": ev["event_id"], "status": "duplicate" if not created else "archived", "path": rel}
                continue
            if pair in seen or (legacy and ev["message"]["text"] and _norm(ev["message"]["text"]) in body_norm):
                res[i] = {"event_id": ev["event_id"], "status": "duplicate", "path": rel}
                continue
            revision = ev["message_key"] in keys
            blocks.append(_block(ev, revision))
            seen.add(pair)
            keys.add(ev["message_key"])
            res[i] = {"event_id": ev["event_id"], "status": "archived", "path": rel, **({"revision": True} if revision else {})}
        if blocks:
            chunk = ("" if text.endswith("\n") else "\n") + "".join(blocks)
            with open(p, "a", encoding="utf-8", newline="\n") as f:  # append only: nothing above is touched
                f.write(chunk)
                f.flush()
                os.fsync(f.fileno())
            with b._lock:
                b._index_one(p, p.stat())
                b.db.commit()
            n = len(blocks)
            b.log(agent, "archived", rel, f"{n} message{'s' if n != 1 else ''} from {display_name(provider)}")
            b.emit({"type": "changed", "actor": agent, "paths": [rel], "archive": True})
        return [res[i] for i in range(len(evs))]


def _first_line(text: str) -> str:
    return next((l.strip() for l in (text or "").splitlines() if l.strip()), "")[:80]


def _yaml(s: str) -> str:
    import json
    return json.dumps(s, ensure_ascii=False)


def _block(ev: dict, revision: bool) -> str:
    from cortex_gateway.schema import display_name
    m = ev["message"]
    role = m["role"]
    who = {"user": "You", "assistant": AI_NAMES.get(display_name(ev["provider"]), display_name(ev["provider"])),
           "tool": "Tool", "system": "System"}.get(role, role.title())
    if ev["kind"] == "agent_event":
        who = f"{who} · event"
    when = m["ts"][:16].replace("T", " ") if m["ts"] else ""
    head = f"**{who}**" + (" (revised)" if revision else "") + (f" · {when}" if when else "") + \
        (f" · {m['model']}" if m.get("model") else "") + f" <!-- m:{ev['message_key']} h:{ev['content_hash']} -->"
    text = ANCHOR.sub("<!-- quoted anchor -->", m["text"]).rstrip()  # message text can't forge a dedup anchor
    parts = [head, "", text] if text else [head]
    for a in ev.get("attachments", []):
        meta = ", ".join(x for x in (a["mime"], f"{a['size']:,} bytes" if a["size"] is not None else "", f"sha256 {a['sha256'][:12]}" if a["sha256"] else "") if x)
        parts.append(f"- attachment: {a['name']}" + (f" ({meta})" if meta else "") + (f" <{a['uri']}>" if a["uri"] else ""))
    return "\n" + "\n".join(parts) + "\n"
