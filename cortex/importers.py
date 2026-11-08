"""Import full conversation history from AI apps into the brain.

Every AI app already keeps your conversations on your own disk. Cortex reads
them (read-only), turns each conversation into one Markdown note under
Conversations/<App>/<YYYY-MM>/, and keeps them up to date incrementally: a
session is only re-read when its file changed.

What is kept: everything you typed, and every reply, in full. Tool calls are
summarised to one line ("used Bash: npm test"); tool output, hidden reasoning
and system boilerplate are dropped, which typically shrinks a transcript 10-50x.
Obvious secrets (API keys, tokens, private keys, passwords) are redacted.

Sources:
  Claude Code   ~/.claude/projects/*/*.jsonl                      (verified)
  Codex CLI     ~/.codex/sessions/**/*.jsonl                       (documented format)
  Gemini CLI    ~/.gemini/tmp/*/chats/*.json, logs.json            (documented format)
  Cursor        .../Cursor/User/globalStorage/state.vscdb          (experimental)
  ChatGPT       Settings > Data controls > Export   -> zip/conversations.json
  Claude.ai     Settings > Privacy > Export data    -> zip/conversations.json
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
import time
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

HOME = Path.home()
# Same invisible-character set the research module strips (prompt-smuggling vectors).
INVISIBLE = re.compile("[\u200b\u200c\u200e\u200f\u202a-\u202e\u2060-\u2064\u2066-\u2069\ufeff\U000e0000-\U000e007f]")

SECRET_PATTERNS = [
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S), "[REDACTED PRIVATE KEY]"),
    (re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}"), "[REDACTED]"),
    (re.compile(r"\bsk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{20,}"), "[REDACTED]"),
    (re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,})"), "[REDACTED]"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "[REDACTED]"),
    (re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"), "[REDACTED]"),
    (re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}"), "[REDACTED]"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"), "[REDACTED JWT]"),
    (re.compile(r"(?i)(\bbearer\s+)[A-Za-z0-9._~+/=-]{20,}"), r"\1[REDACTED]"),
    (re.compile(r"(?i)\b((?:password|passwd|pwd|secret|api[_-]?key|access[_-]?token)\s*[:=]\s*)(['\"]?)[^\s'\"]{6,}\2"), r"\1[REDACTED]"),
]


def redact(text: str) -> str:
    for pat, repl in SECRET_PATTERNS:
        text = pat.sub(repl, text)
    return text


@dataclass
class Message:
    role: str  # "user" | "assistant"
    text: str
    ts: str = ""


@dataclass
class Conversation:
    source: str  # display name, e.g. "Claude Code"
    key: str  # stable id used for incremental state
    sig: str  # changes whenever the conversation changes
    title: str = ""
    started: str = ""
    updated: str = ""
    project: str = ""
    messages: list[Message] = field(default_factory=list)

    def add(self, role: str, text: str, ts: str = "") -> None:
        text = (text or "").strip()
        if not text:
            return
        if self.messages and self.messages[-1].role == role and role == "assistant":
            self.messages[-1].text += "\n\n" + text  # one turn split across several log lines
        else:
            self.messages.append(Message(role, text, ts))


# ── helpers ─────────────────────────────────────────────────────────────
def _iso(v) -> str:
    if v in (None, ""):
        return ""
    if isinstance(v, (int, float)):
        if v > 1e12:  # milliseconds
            v /= 1000
        return datetime.fromtimestamp(v, timezone.utc).astimezone().isoformat(timespec="seconds")
    s = str(v)
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone().isoformat(timespec="seconds")
    except ValueError:
        return s


def _file_sig(p: Path) -> str:
    st = p.stat()
    return f"{st.st_size}:{int(st.st_mtime)}"


def _tool_line(name: str, args) -> str:
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except ValueError:
            args = {"input": args}
    detail = ""
    if isinstance(args, dict):
        for k in ("command", "cmd", "file_path", "path", "pattern", "query", "url", "description", "prompt"):
            if args.get(k):
                v = args[k]
                detail = " ".join(v) if isinstance(v, list) else str(v)
                break
    detail = " ".join(detail.split())[:160]
    return f"> used {name}" + (f": `{detail}`" if detail else "")


SYSTEM_TAGS = re.compile(
    r"<(system-reminder|local-command-caveat|local-command-stdout|local-command-stderr|command-message|"
    r"environment_context|user_instructions|ide_opened_file|ide_selection)>.*?</\1>", re.S)


def _clean_user(text: str) -> str:
    cmd = re.search(r"<command-name>(.*?)</command-name>", text, re.S)
    if cmd:
        args = re.search(r"<command-args>(.*?)</command-args>", text, re.S)
        return f"{cmd.group(1).strip()} {args.group(1).strip() if args else ''}".strip()
    return SYSTEM_TAGS.sub("", text).strip()


def _title_from(c: Conversation) -> str:
    if c.title:
        return c.title
    for m in c.messages:
        if m.role == "user":
            line = next((l for l in m.text.splitlines() if l.strip()), "")
            return line.strip()[:80]
    return "Untitled conversation"


# ── Claude Code ─────────────────────────────────────────────────────────
def claude_code_files() -> list[Path]:
    base = Path(os.environ.get("CLAUDE_CONFIG_DIR", HOME / ".claude")) / "projects"
    return sorted(base.glob("*/*.jsonl")) if base.is_dir() else []


def parse_claude_code(p: Path) -> Conversation | None:
    c = Conversation("Claude Code", f"claude-code:{p.stem}", _file_sig(p))
    summary = ""
    with open(p, encoding="utf-8", errors="replace") as f:
        for line in f:
            try:
                o = json.loads(line)
            except ValueError:
                continue
            t = o.get("type")
            if t == "summary" and o.get("summary"):
                summary = o["summary"]
            if t not in ("user", "assistant") or o.get("isSidechain") or o.get("isMeta"):
                continue
            ts = o.get("timestamp", "")
            c.started = c.started or ts
            c.updated = ts or c.updated
            c.project = c.project or o.get("cwd", "")
            content = (o.get("message") or {}).get("content")
            if t == "user":
                if isinstance(content, str):
                    c.add("user", _clean_user(content), ts)
                elif isinstance(content, list):
                    parts = []
                    for b in content:
                        if b.get("type") == "text":
                            parts.append(_clean_user(b.get("text", "")))
                        elif b.get("type") == "image":
                            parts.append("[image]")
                    c.add("user", "\n".join(x for x in parts if x), ts)
            else:
                parts = []
                for b in content if isinstance(content, list) else [{"type": "text", "text": content or ""}]:
                    if b.get("type") == "text":
                        parts.append(b.get("text", ""))
                    elif b.get("type") == "tool_use":
                        parts.append(_tool_line(b.get("name", "tool"), b.get("input")))
                c.add("assistant", "\n".join(x for x in parts if x), ts)
    c.title = summary
    return c if any(m.role == "user" for m in c.messages) else None


# ── Codex CLI ───────────────────────────────────────────────────────────
def codex_files() -> list[Path]:
    base = Path(os.environ.get("CODEX_HOME", HOME / ".codex")) / "sessions"
    return sorted(base.rglob("*.jsonl")) if base.is_dir() else []


CODEX_SKIP = ("<environment_context>", "<user_instructions>", "# AGENTS.md instructions", "<permissions instructions>")


def parse_codex(p: Path) -> Conversation | None:
    c = Conversation("Codex", f"codex:{p.stem}", _file_sig(p))
    with open(p, encoding="utf-8", errors="replace") as f:
        for line in f:
            try:
                o = json.loads(line)
            except ValueError:
                continue
            ts = o.get("timestamp", "")
            kind = o.get("type")
            pl = o.get("payload") if isinstance(o.get("payload"), dict) else o  # new vs old format
            if kind == "session_meta" or ("instructions" in o and "id" in o and "type" not in o):
                c.started = c.started or pl.get("timestamp", ts)
                c.project = c.project or pl.get("cwd", "")
                continue
            if kind == "event_msg":
                continue  # duplicates of response items
            ptype = pl.get("type")
            if ptype == "message" and pl.get("role") in ("user", "assistant"):
                text = "\n".join(b.get("text", "") for b in pl.get("content") or [] if isinstance(b, dict)
                                 and b.get("type") in ("input_text", "output_text", "text"))
                if pl["role"] == "user" and text.lstrip().startswith(CODEX_SKIP):
                    continue
                c.add(pl["role"], _clean_user(text) if pl["role"] == "user" else text, ts)
            elif ptype in ("function_call", "custom_tool_call", "local_shell_call"):
                c.add("assistant", _tool_line(pl.get("name") or "shell", pl.get("arguments") or pl.get("input") or pl.get("action", {})), ts)
            if ts:
                c.started = c.started or ts
                c.updated = ts
    return c if any(m.role == "user" for m in c.messages) else None


# ── Gemini CLI ──────────────────────────────────────────────────────────
def gemini_files() -> list[Path]:
    base = HOME / ".gemini" / "tmp"
    if not base.is_dir():
        return []
    return sorted(base.glob("*/chats/*.json")) + sorted(base.glob("*/logs.json"))


def _gem_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(x.get("text", "") if isinstance(x, dict) else str(x) for x in content)
    if isinstance(content, dict):
        return content.get("text", "")
    return ""


def parse_gemini(p: Path) -> list[Conversation]:
    try:
        data = json.loads(p.read_text(encoding="utf-8", errors="replace"))
    except ValueError:
        return []
    sig = _file_sig(p)
    if p.name == "logs.json" and isinstance(data, list):  # prompts only (older Gemini CLI)
        by_session: dict[str, Conversation] = {}
        for e in data:
            if e.get("type") != "user":
                continue
            sid = str(e.get("sessionId", "unknown"))
            c = by_session.setdefault(sid, Conversation("Gemini CLI", f"gemini:{sid}", sig, project=p.parent.name))
            c.add("user", e.get("message", ""), e.get("timestamp", ""))
            c.started = c.started or e.get("timestamp", "")
            c.updated = e.get("timestamp", c.updated)
        return [c for c in by_session.values() if c.messages]
    if not isinstance(data, dict):
        return []
    sid = str(data.get("sessionId") or p.stem)
    c = Conversation("Gemini CLI", f"gemini:{sid}", sig, started=data.get("startTime", ""),
                     updated=data.get("lastUpdated", ""), project=data.get("projectHash", p.parent.parent.name))
    for m in data.get("messages") or []:
        role = {"user": "user", "gemini": "assistant", "model": "assistant"}.get(m.get("type") or m.get("role"))
        if not role:
            continue
        text = _gem_text(m.get("content"))
        if role == "assistant":
            calls = [_tool_line(tc.get("name", "tool"), tc.get("args")) for tc in m.get("toolCalls") or []]
            text = "\n".join([text] + calls)
        c.add(role, text, m.get("timestamp", ""))
    return [c] if any(m.role == "user" for m in c.messages) else []


# ── Cursor (experimental: Cursor changes its storage often) ─────────────
def cursor_db() -> Path | None:
    if os.name == "nt":
        base = Path(os.environ.get("APPDATA", HOME / "AppData" / "Roaming")) / "Cursor"
    elif sys.platform == "darwin":
        base = HOME / "Library" / "Application Support" / "Cursor"
    else:
        base = HOME / ".config" / "Cursor"
    p = base / "User" / "globalStorage" / "state.vscdb"
    return p if p.is_file() else None


def parse_cursor(p: Path) -> list[Conversation]:
    try:
        db = sqlite3.connect(f"file:{p.as_posix()}?mode=ro", uri=True, timeout=5)
        rows = db.execute("SELECT key, value FROM cursorDiskKV WHERE key LIKE 'composerData:%'").fetchall()
    except sqlite3.Error:
        return []
    out = []
    try:
        for key, value in rows:
            try:
                d = json.loads(value)
            except (TypeError, ValueError):
                continue
            cid = d.get("composerId") or key.split(":", 1)[1]
            sig = str(d.get("lastUpdatedAt") or d.get("createdAt") or len(value))
            c = Conversation("Cursor", f"cursor:{cid}", sig, title=d.get("name") or "",
                             started=_iso(d.get("createdAt")), updated=_iso(d.get("lastUpdatedAt")))
            bubbles = d.get("conversation") or []
            if not bubbles and d.get("fullConversationHeadersOnly"):
                for h in d["fullConversationHeadersOnly"]:
                    r = db.execute("SELECT value FROM cursorDiskKV WHERE key=?", (f"bubbleId:{cid}:{h.get('bubbleId')}",)).fetchone()
                    if r:
                        try:
                            bubbles.append(json.loads(r[0]))
                        except (TypeError, ValueError):
                            pass
            for b in bubbles:
                role = {1: "user", 2: "assistant"}.get(b.get("type"))
                if role:
                    c.add(role, b.get("text") or b.get("richText") or "")
            if any(m.role == "user" for m in c.messages):
                out.append(c)
    finally:
        db.close()
    return out


# ── exports from the web apps ───────────────────────────────────────────
def _load_export(path: Path):
    if path.suffix.lower() == ".zip":
        with zipfile.ZipFile(path) as z:
            name = next((n for n in z.namelist() if n.endswith("conversations.json")), None)
            if not name:
                raise ValueError("no conversations.json in this zip")
            return json.loads(z.read(name).decode("utf-8", errors="replace"))
    return json.loads(path.read_text(encoding="utf-8", errors="replace"))


def parse_export(path: Path) -> list[Conversation]:
    try:
        data = _load_export(path)
    except (ValueError, zipfile.BadZipFile) as e:
        raise ValueError(f"not a ChatGPT/Claude.ai export: {e}")
    if not isinstance(data, list):
        raise ValueError("unrecognised export (expected a ChatGPT or Claude.ai conversations.json)")
    if not data:
        return []
    first = data[0]
    if "mapping" in first:
        return [c for c in (_chatgpt(x) for x in data) if c]
    if "chat_messages" in first:
        return [c for c in (_claude_ai(x) for x in data) if c]
    raise ValueError("unrecognised export (expected a ChatGPT or Claude.ai conversations.json)")


def _chatgpt(x: dict) -> Conversation | None:
    cid = x.get("conversation_id") or x.get("id") or str(x.get("create_time"))
    c = Conversation("ChatGPT", f"chatgpt:{cid}", str(x.get("update_time")), title=x.get("title") or "",
                     started=_iso(x.get("create_time")), updated=_iso(x.get("update_time")))
    mapping = x.get("mapping") or {}
    node, chain = x.get("current_node"), []
    while node and node in mapping and len(chain) < 100000:
        chain.append(mapping[node])
        node = mapping[node].get("parent")
    for n in reversed(chain):
        m = n.get("message") or {}
        role = (m.get("author") or {}).get("role")
        if role not in ("user", "assistant") or (m.get("metadata") or {}).get("is_visually_hidden_from_conversation"):
            continue
        content = m.get("content") or {}
        if content.get("content_type") not in ("text", "multimodal_text"):
            continue
        parts = [p if isinstance(p, str) else "[image]" for p in content.get("parts") or []]
        c.add(role, "\n".join(parts), _iso(m.get("create_time")))
    return c if c.messages else None


def _claude_ai(x: dict) -> Conversation | None:
    c = Conversation("Claude.ai", f"claude-ai:{x.get('uuid')}", str(x.get("updated_at")), title=x.get("name") or "",
                     started=_iso(x.get("created_at")), updated=_iso(x.get("updated_at")))
    for m in x.get("chat_messages") or []:
        role = {"human": "user", "assistant": "assistant"}.get(m.get("sender"))
        if not role:
            continue
        blocks = [b.get("text", "") for b in m.get("content") or [] if isinstance(b, dict) and b.get("type") == "text"]
        c.add(role, "\n".join(blocks) if blocks else m.get("text", ""), _iso(m.get("created_at")))
    return c if c.messages else None


# ── rendering & storing ─────────────────────────────────────────────────
AI_NAMES = {"Claude Code": "Claude", "Claude.ai": "Claude", "Codex": "Codex", "Gemini CLI": "Gemini",
            "Cursor": "Cursor", "ChatGPT": "ChatGPT"}


def _safe(s: str, n: int = 60, max_bytes: int = 90) -> str:
    """A title usable as a file name on every system: no reserved characters, and short in
    BYTES too (Japanese/Arabic/emoji take 3-4 bytes per character)."""
    s = re.sub(r'[\\/:*?"<>|#^\[\]\x00-\x1f]+', " ", s)
    s = " ".join(s.split())[:n]
    while len(s.encode("utf-8")) > max_bytes:
        s = s[:-1]
    s = s.rstrip(" .")
    if s.split(".")[0].strip().upper() in {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}:
        s = "_" + s
    return s or "Untitled"


def note_path(c: Conversation) -> str:
    started = _iso(c.started) or _iso(time.time())
    day, hm = started[:10], started[11:16].replace(":", "")
    ident = re.sub(r"[^A-Za-z0-9]", "", c.key.split(":", 1)[1])[-6:] or "x"
    return f"Conversations/{c.source}/{day[:7]}/{day} {hm} {_safe(_title_from(c))} ({ident}).md"


def render(c: Conversation) -> str:
    title = _title_from(c)
    you, ai = "You", AI_NAMES.get(c.source, c.source)
    fm = ["---", f"tags: [conversation, {re.sub(r'[^a-z0-9]+', '-', c.source.lower()).strip('-')}]",
          f"source: {c.source}", f"session: {c.key.split(':', 1)[1]}"]
    if c.project:
        fm.append(f"project: {json.dumps(c.project)}")
    fm += [f"started: {_iso(c.started)}", f"updated: {_iso(c.updated) or _iso(c.started)}",
           f"messages: {len(c.messages)}", "---", f"# {' '.join(title.split())[:120]}", ""]
    out = fm
    for m in c.messages:
        when = _iso(m.ts)[:16].replace("T", " ")
        out.append(f"**{you if m.role == 'user' else ai}**" + (f" · {when}" if when else ""))
        out.append("")
        out.append(redact(INVISIBLE.sub("", m.text)))
        out.append("")
    return "\n".join(out)


class Importer:
    def __init__(self, brain):
        self.brain = brain
        # Per machine: each PC imports its own AI apps' history (local paths, local lock).
        self.state_file = brain.local_dir / "imports.json"
        self.lock_file = brain.local_dir / "import.lock"

    def _load(self) -> dict:
        try:
            return json.loads(self.state_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _save(self, state: dict) -> None:
        tmp = self.state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(state), encoding="utf-8")
        os.replace(tmp, self.state_file)

    def _acquire(self) -> bool:
        try:
            fd = os.open(self.lock_file, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            return True
        except FileExistsError:
            try:
                if time.time() - self.lock_file.stat().st_mtime > 600:  # stale lock
                    self.lock_file.unlink()
                    return self._acquire()
            except OSError:
                pass
            return False

    def _local_conversations(self, state: dict, sources: set[str] | None):
        """Yield conversations from local apps, skipping files that haven't changed."""
        def want(name):
            return sources is None or name in sources

        file_sources = []
        if want("claude-code"):
            file_sources.append((claude_code_files(), lambda p: [parse_claude_code(p)]))
        if want("codex"):
            file_sources.append((codex_files(), lambda p: [parse_codex(p)]))
        if want("gemini"):
            file_sources.append((gemini_files(), parse_gemini))
        for files, parse in file_sources:
            for p in files:
                fkey = f"file:{p}"
                try:
                    sig = _file_sig(p)
                except OSError:
                    continue
                if state.get(fkey) == sig:
                    continue
                try:
                    convs = [c for c in parse(p) if c]
                except Exception as e:  # one bad file must not stop the rest
                    print(f"cortex import: skipped {p.name}: {e}", file=sys.stderr)
                    continue
                yield from convs
                state[fkey] = sig
        if want("cursor"):
            db = cursor_db()
            if db:
                fkey = f"file:{db}"
                sig = _file_sig(db)
                if state.get(fkey) != sig:
                    yield from parse_cursor(db)
                    state[fkey] = sig

    def run(self, sources: set[str] | None = None, export: Path | None = None) -> dict:
        if not self._acquire():
            return {"skipped": "another import is running"}
        try:
            state = self._load()
            notes = state.setdefault("notes", {})
            counts: dict[str, int] = {}
            convs = parse_export(export) if export else self._local_conversations(state, sources)
            new = updated = 0
            for c in convs:
                prev = notes.get(c.key)
                if prev and prev.get("sig") == c.sig and (self.brain.root / prev["path"]).exists():
                    continue
                rel = prev["path"] if prev else note_path(c)
                try:
                    self.brain.store(rel, render(c))
                except (ValueError, OSError) as e:  # one odd conversation must not stop the import
                    print(f"cortex import: skipped {c.source} {c.key}: {e}", file=sys.stderr)
                    continue
                notes[c.key] = {"sig": c.sig, "path": rel}
                counts[c.source] = counts.get(c.source, 0) + 1
                if prev:
                    updated += 1
                else:
                    new += 1
                if (new + updated) % 200 == 0:
                    self._save(state)  # progress survives an interruption
            state["last_run"] = _iso(time.time())
            self._save(state)
            if new or updated:
                self.brain.log("cortex", "imported", "Conversations/",
                               f"{new} new, {updated} updated conversation(s): "
                               + ", ".join(f"{k} {v}" for k, v in counts.items()))
            return {"new": new, "updated": updated, "by_source": counts, "last_run": state["last_run"]}
        finally:
            try:
                self.lock_file.unlink()
            except OSError:
                pass

    def status(self) -> dict:
        state = self._load()
        by_source: dict[str, int] = {}
        for k in state.get("notes", {}):
            src = k.split(":", 1)[0]
            by_source[src] = by_source.get(src, 0) + 1
        detected = {"claude-code": bool(claude_code_files()), "codex": bool(codex_files()),
                    "gemini": bool(gemini_files()), "cursor": bool(cursor_db())}
        return {"last_run": state.get("last_run"), "conversations": by_source, "detected": detected}
