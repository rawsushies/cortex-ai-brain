"""Cortex core: the vault (Markdown files) plus a fast SQLite index.

Design:
- The vault is the single source of truth: plain Markdown, Obsidian-compatible,
  readable by any AI or human. Cortex never stores note content anywhere else
  except as a disposable search index and a version history.
- The index lives in <vault>/.cortex/index.db (dot folder, hidden from Obsidian).
  It uses SQLite FTS5 (BM25 ranking) and is refreshed incrementally: a sync stats
  every file and re-reads only the ones whose mtime/size changed.
- Every change made through Cortex first snapshots the old version into
  .cortex/history, so any AI mistake can be undone from the app.
- Activity (who wrote what) is an append-only JSONL file, so every AI host shares
  one logbook, in the spirit of obsidian-second-brain's "Crew".
- Optional semantic search (local Ollama embeddings) lives in semantic.py and is
  fused with keyword search; without it everything still works.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import socket
import sys
import shutil
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path

SCHEMA_VERSION = 5  # bump when the index layout changes; the index then rebuilds itself
SKIP_DIRS = {".cortex", ".obsidian", ".git", ".trash", "node_modules", ".venv"}
# Root-level files each AI host reads on its own; plumbing, not notes.
POINTER_FILES = {"agents.md", "claude.md", "gemini.md"}
SYSTEM_NOTES = {"_cortex.md", "home.md"}
PROTECTED_NOTES = {"_cortex.md", "agents.md", "claude.md", "gemini.md"}  # AI rules: user-only
HUMAN_AGENTS = {"you", "human"}
WINDOWS_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
MD_NOISE = re.compile(r"^#{1,6}\s+|\*\*|__|`|\[\[|\]\]|^>\s?|^[-*]\s+\[[ xX]\]\s+", re.M)
WIKILINK = re.compile(r"\[\[([^\]|#^]+)(?:[#^][^\]|]*)?(?:\|[^\]]*)?\]\]")
WIKILINK_FULL = re.compile(r"(!?)\[\[([^\]|#^]+)((?:[#^][^\]|]*)?)((?:\|[^\]]*)?)\]\]")
TAG = re.compile(r"(?:^|\s)#([A-Za-z][\w/-]*)")
HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$", re.M)
TASK = re.compile(r"^(\s*[-*+]\s+\[)([ xX])(\]\s+)(.*)$")
DUE = re.compile(r"(?:📅\s*|due:\s*|@)(\d{4}-\d{2}-\d{2})")
SYNC_INTERVAL = 2.0  # seconds between disk re-checks; grows with vault size (see sync)
SNIPPET_CHARS = 170
MAX_NAME_BYTES = 150  # per file/folder name (file systems allow 255; leave room)
MAX_REL_PATH = 200  # vault-relative; keeps full paths under Windows' 260-character limit
HEADING_WEIGHT = 6  # recall: a query word in a passage heading counts like this many mentions
HISTORY_KEEP = 30  # versions kept per note
ACTIVE_STATUSES = {"active", "in progress", "in-progress", "doing", "ongoing", "open"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS notes(
  id INTEGER PRIMARY KEY, path TEXT UNIQUE, title TEXT, mtime REAL, size INTEGER,
  tags TEXT, words INTEGER, status TEXT, created REAL
);
CREATE TABLE IF NOT EXISTS links(src TEXT, dst TEXT, dst_l TEXT);
CREATE INDEX IF NOT EXISTS links_src ON links(src);
CREATE INDEX IF NOT EXISTS links_dst_l ON links(dst_l);
CREATE INDEX IF NOT EXISTS notes_mtime ON notes(mtime);
CREATE TABLE IF NOT EXISTS tasks(path TEXT, line INTEGER, text TEXT, done INTEGER, due TEXT);
CREATE INDEX IF NOT EXISTS tasks_path ON tasks(path);
"""
# Best first. Contentless + column detail stores ~0.5x the vault's text (the old layout
# kept a full copy: ~2.4x). Snippets are cut from the files on disk instead.
# contentless_delete needs SQLite 3.43+ (2023); older Pythons fall back gracefully.
FTS_LAYOUTS = (
    "CREATE VIRTUAL TABLE IF NOT EXISTS fts USING fts5(title, tags, body, content='', "
    "contentless_delete=1, detail=column, tokenize='porter unicode61')",
    "CREATE VIRTUAL TABLE IF NOT EXISTS fts USING fts5(title, tags, body, tokenize='porter unicode61')",
)


DEVICE = re.sub(r"[^A-Za-z0-9_-]", "-", (os.environ.get("CORTEX_DEVICE") or socket.gethostname() or "device"))[:40].strip("-") or "device"


def local_state_dir(vault: Path) -> Path:
    """Per-machine folder for the index and import state, outside any synced vault."""
    override = os.environ.get("CORTEX_LOCAL_DIR")
    if override:
        base = Path(override)
    elif os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "Cortex"
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support" / "Cortex"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "cortex"
    key = hashlib.sha256(str(vault).lower().encode() if os.name == "nt" else str(vault).encode()).hexdigest()[:16]
    return base / "vaults" / f"{re.sub(r'[^A-Za-z0-9_-]', '_', vault.name)[:30]}-{key}"


def _ts_key(ts: str) -> float:
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return 0.0


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def split_frontmatter(text: str) -> tuple[dict, str]:
    """Parse a simple YAML frontmatter block (key: value, key: [a, b], - item lists)."""
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end == -1:
        return {}, text
    meta: dict = {}
    last = None
    for line in text[3:end].strip().splitlines():
        s = line.strip()
        if s.startswith("- ") and last and isinstance(meta.get(last), list):
            meta[last].append(s[2:].strip().strip("'\""))
            continue
        if ":" not in line or line.startswith((" ", "\t")):
            continue
        key, _, val = line.partition(":")
        key, val = key.strip(), val.strip()
        last = key
        if val.startswith("[") and val.endswith("]"):
            meta[key] = [v.strip().strip("'\"") for v in val[1:-1].split(",") if v.strip()]
        elif val == "":
            meta[key] = []  # a "- item" list may follow
        else:
            meta[key] = val.strip("'\"")
    body = text[end + 4:]
    return meta, body.lstrip("\n")


def note_title(path: str, meta: dict, body: str) -> str:
    if meta.get("title") and isinstance(meta["title"], str):
        return meta["title"]
    m = HEADING.search(body)
    if m and len(m.group(1)) == 1:
        return m.group(2).strip()
    return Path(path).stem


def note_tags(meta: dict, body: str) -> list[str]:
    tags = meta.get("tags", [])
    if isinstance(tags, str):
        tags = [t.strip() for t in re.split(r"[,\s]+", tags) if t.strip()]
    found = set(t.lstrip("#") for t in tags if t)
    clean = re.sub(r"```.*?```", "", body, flags=re.S)  # ignore tags inside code
    found.update(TAG.findall(clean))
    return sorted(found)


# Filler words carry no meaning for retrieval (English + Malay, since questions come in both).
STOPWORDS = set("""a an and are as at be been but by can could did do does doing for from had has have how i if in into
is it its me my of on or our should so than that the their them then there these they this to was we were what when where
which who whom why will with would you your about any all also am just more most no not only other same some such too very
yang dan di ke dari untuk apa macam mana saya aku kita kami ini itu ada tak tidak boleh dengan pada atau juga lagi nak ke
""".split())


def content_words(q: str) -> list[str]:
    words = re.findall(r"\w+", q, flags=re.UNICODE)
    keep = [w for w in words if w.lower() not in STOPWORDS]
    return keep or words  # a query made only of stopwords still searches for them


def fts_query(q: str, any_word: bool = False) -> str:
    """Turn free text into a safe FTS5 query: content words, prefix-matched."""
    return (" OR " if any_word else " ").join(f'"{w}"*' for w in content_words(q))


def norm_title(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", s.lower())


@dataclass
class Hit:
    path: str
    title: str
    snippet: str
    score: float
    via: list = field(default_factory=list)  # "keyword", "meaning"


class Brain:
    """One vault + its index. Thread-safe; safe to share across processes (WAL)."""

    def __init__(self, vault: str | os.PathLike, semantic: bool | None = None):
        self.root = Path(vault).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        # Synced with the vault (safe for Google Drive/OneDrive/Dropbox): history, per-device logs.
        self.meta_dir = self.root / ".cortex"
        self.meta_dir.mkdir(exist_ok=True)
        self.history_dir = self.meta_dir / "history"
        self.activity_dir = self.meta_dir / "activity"
        # Machine-local (never synced): the SQLite index and import state. A database file must
        # not live in a cloud-synced folder: sync tools copy it mid-write and corrupt it, and two
        # PCs would fight over it. It rebuilds from the notes, so keeping it local loses nothing.
        self.local_dir = local_state_dir(self.root)
        self.local_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._last_sync = 0.0
        self._names: dict[str, str] | None = None
        self._text_cache: dict[str, tuple[float, str]] = {}
        self._recall_cache: dict[tuple, dict] = {}
        self.db = self._open_index()
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        if self.db.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
            for t in ("notes", "links", "tasks", "fts", "emb"):
                self.db.execute(f"DROP TABLE IF EXISTS {t}")
            self.db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        self.db.executescript(SCHEMA)
        self.has_fts = False
        for ddl in FTS_LAYOUTS:
            try:
                self.db.execute(ddl)
                self.has_fts = True
                break
            except sqlite3.OperationalError:
                continue
        if not self.has_fts:  # SQLite built without FTS5: plain table + LIKE search
            self.db.execute("CREATE TABLE IF NOT EXISTS fts(title TEXT, tags TEXT, body TEXT)")
        self.db.commit()
        self._sync_every = SYNC_INTERVAL
        from .semantic import Semantic
        self.semantic = Semantic(self, enabled=semantic)

    def _open_index(self) -> sqlite3.Connection:
        """Open the index; if it is corrupt (power cut, disk error), set it aside and rebuild.
        The notes are the source of truth, so nothing is lost."""
        path = self.local_dir / "index.db"
        for old in ("index.db", "index.db-wal", "index.db-shm"):  # pre-1.4 location inside the (maybe synced) vault
            try:
                (self.meta_dir / old).unlink()
            except OSError:
                pass
        for attempt in (1, 2):
            db = sqlite3.connect(path, check_same_thread=False, timeout=15)
            try:
                db.execute("PRAGMA schema_version").fetchone()
                db.execute("SELECT count(*) FROM sqlite_master").fetchone()
                return db
            except sqlite3.DatabaseError:
                db.close()
                if attempt == 2:
                    raise
                stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
                for suffix in ("", "-wal", "-shm"):
                    f = self.local_dir / f"index.db{suffix}"
                    if f.exists():
                        os.replace(f, self.local_dir / f"index.db{suffix}.corrupt-{stamp}")
        raise RuntimeError("unreachable")

    # ── paths ────────────────────────────────────────────────────────────
    def resolve(self, rel: str) -> Path:
        """Map a vault-relative path to disk, refusing anything outside the vault."""
        rel = rel.replace("\\", "/").strip().lstrip("/")
        if not rel or "\x00" in rel:
            raise ValueError("empty path")
        if not rel.lower().endswith(".md"):
            rel += ".md"
        if len(rel) > MAX_REL_PATH:
            raise ValueError(f"path too long (max {MAX_REL_PATH} characters; Windows can't handle longer paths)")
        for part in rel.split("/"):
            if len(part.encode("utf-8")) > MAX_NAME_BYTES:
                raise ValueError(f"name too long: {part[:40]}… (max {MAX_NAME_BYTES} bytes per folder or file name)")
            if part.lower() in SKIP_DIRS:  # case-insensitive: ".CORTEX" is ".cortex" on Windows/macOS
                raise ValueError("path is inside a reserved folder")
            stem = part.split(".")[0].strip().upper()
            if stem in WINDOWS_RESERVED or re.search(r'[<>:"|?*\x00-\x1f]', part) or part != part.rstrip(". "):
                raise ValueError(f"{part!r} isn't a valid file name on every system")
        p = (self.root / rel).resolve()
        if p != self.root and self.root not in p.parents:
            raise ValueError("path escapes the vault")
        if any(part.lower() in SKIP_DIRS for part in p.relative_to(self.root).parts):
            raise ValueError("path is inside a reserved folder")
        return p

    def check_agent_write(self, rel: str, agent: str) -> None:
        """AIs may not rewrite the rules every AI follows: a poisoned web page could
        otherwise persist instructions into all future sessions. Only you can, in the app."""
        if agent not in HUMAN_AGENTS and Path(rel).name.lower() in PROTECTED_NOTES and "/" not in rel:
            raise PermissionError(f"{rel} holds the rules for every AI; only the user can change it (in the Cortex app)")
        if agent not in HUMAN_AGENTS and rel.startswith("Conversations/"):
            raise PermissionError("Conversations/ is imported history; it can't be edited by an AI")

    def rel(self, p: Path) -> str:
        return p.relative_to(self.root).as_posix()

    def find(self, name: str) -> str | None:
        """Resolve a note by path, filename or title (how [[wikilinks]] work)."""
        self.sync()
        name = name.strip()
        try:
            p = self.resolve(name)
            if p.exists():
                return self.rel(p)
        except ValueError:
            pass
        return self._link_target(name)

    def _must_find(self, name: str) -> str:
        found = self.find(name)
        if not found:
            raise FileNotFoundError(f"no note named {name!r}")
        return found

    # ── indexing ─────────────────────────────────────────────────────────
    def _walk(self):
        for dirpath, dirnames, filenames in os.walk(self.root):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
            dirnames[:] = [d for d in dirnames if d.lower() not in SKIP_DIRS and not os.path.islink(os.path.join(dirpath, d))]
            at_root = Path(dirpath) == self.root
            for f in filenames:
                full = os.path.join(dirpath, f)
                # Symlinks are skipped: a link to ~/.ssh/id_rsa must not get indexed and searchable.
                if f.lower().endswith(".md") and not (at_root and f.lower() in POINTER_FILES) and not os.path.islink(full):
                    yield Path(full)

    def _index_one(self, p: Path, st: os.stat_result) -> None:
        rel = self.rel(p)
        if p.parent == self.root and p.name.lower() in POINTER_FILES:
            return
        text = p.read_text(encoding="utf-8", errors="replace")
        meta, body = split_frontmatter(text)
        title = note_title(rel, meta, body)
        tags = note_tags(meta, body)
        no_code = re.sub(r"`[^`\n]*`", "", re.sub(r"```.*?```", "", body, flags=re.S))
        links = {m.strip() for m in WIKILINK.findall(no_code)}  # [[examples]] in code aren't links
        status = str(meta.get("status") or "").strip().lower()
        # Line numbers are counted in the full text so toggling edits the right line.
        tasks, in_code = [], False
        for i, line in enumerate(text.splitlines()):
            if line.lstrip().startswith("```"):
                in_code = not in_code
            m = None if in_code else TASK.match(line)
            if m:
                due = DUE.search(m.group(4))
                tasks.append((rel, i, m.group(4).strip(), int(m.group(2) != " "), due.group(1) if due else None))
        db = self.db
        vals = (title, st.st_mtime, st.st_size, " ".join(tags), len(body.split()), status)
        row = db.execute("SELECT id FROM notes WHERE path=?", (rel,)).fetchone()
        if row:  # the FTS row shares the note's id, so updates are O(1), not a table scan
            nid = row[0]
            db.execute("UPDATE notes SET title=?,mtime=?,size=?,tags=?,words=?,status=? WHERE id=?", (*vals, nid))
            db.execute("DELETE FROM fts WHERE rowid=?", (nid,))
            db.execute("DELETE FROM links WHERE src=?", (rel,))
            db.execute("DELETE FROM tasks WHERE path=?", (rel,))
        else:
            created = getattr(st, "st_birthtime", None) or min(st.st_mtime, st.st_ctime)
            nid = db.execute(
                "INSERT INTO notes(path,title,mtime,size,tags,words,status,created) VALUES(?,?,?,?,?,?,?,?)",
                (rel, *vals, created)).lastrowid
        db.execute("INSERT INTO fts(rowid,title,tags,body) VALUES(?,?,?,?)", (nid, title, " ".join(tags), body))
        db.executemany("INSERT INTO links(src,dst,dst_l) VALUES(?,?,?)", [(rel, l, l.lower()) for l in links])
        db.executemany("INSERT INTO tasks(path,line,text,done,due) VALUES(?,?,?,?,?)", tasks)
        self._names_add(rel, title)

    def _names_add(self, rel: str, title: str) -> None:
        """Keep the link-name map current without rebuilding it (rebuilds cost ~0.3s at 100k notes)."""
        m = self._names
        if m is None:
            return
        for k in (Path(rel).stem.lower(), title.lower()):
            cur = m.get(k)
            if cur is None or len(rel) < len(cur):  # shortest path wins, as in a full rebuild
                m[k] = rel
        m[rel.lower()] = rel
        m[rel[:-3].lower()] = rel

    def _drop(self, rel: str) -> None:
        row = self.db.execute("SELECT id FROM notes WHERE path=?", (rel,)).fetchone()
        if row:
            self.db.execute("DELETE FROM fts WHERE rowid=?", (row[0],))
            self.db.execute("DELETE FROM notes WHERE id=?", (row[0],))
            self.semantic.drop(row[0])
        self.db.execute("DELETE FROM links WHERE src=?", (rel,))
        self.db.execute("DELETE FROM tasks WHERE path=?", (rel,))
        self._names = None

    def _name_map(self) -> dict[str, str]:
        """lowercased path / path-without-.md / filename / title -> note path.
        Built once per index change, so link resolution is a dict lookup."""
        if self._names is None:
            m: dict[str, str] = {}
            rows = self.db.execute("SELECT path,title FROM notes ORDER BY length(path) DESC").fetchall()
            for r in rows:  # shortest path wins (inserted last)
                p = r["path"]
                m[p.rsplit("/", 1)[-1][:-3].lower()] = p  # file name without .md (plain string ops: fast at 100k)
                m[r["title"].lower()] = p
            for r in rows:  # exact paths always win over stems/titles
                m[r["path"].lower()] = r["path"]
                m[r["path"][:-3].lower()] = r["path"]
            self._names = m
        return self._names

    def sync(self, force: bool = False) -> dict:
        """Bring the index up to date. Cheap: stat-only unless files changed."""
        with self._lock:
            if not force and time.monotonic() - self._last_sync < self._sync_every:
                return {"skipped": True}
            started = time.monotonic()
            known = {r["path"]: (r["mtime"], r["size"]) for r in self.db.execute("SELECT path,mtime,size FROM notes")}
            seen, changed = set(), 0
            for p in self._walk():
                try:
                    st = p.stat()
                except OSError:
                    continue
                rel = self.rel(p)
                seen.add(rel)
                if known.get(rel) != (st.st_mtime, st.st_size):
                    try:
                        self._index_one(p, st)
                    except OSError:
                        continue  # file vanished or is locked mid-sync; next sync retries
                    changed += 1
            removed = [r for r in known if r not in seen]
            for r in removed:
                self._drop(r)
            self.db.commit()
            if changed + len(removed) > 2000:  # after a bulk import: compact the index
                if self.has_fts:
                    self.db.execute("INSERT INTO fts(fts) VALUES('optimize')")
                    self.db.commit()
                self.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            self._last_sync = time.monotonic()
            # Changes made through Cortex (any AI, the app) update the shared index instantly;
            # this disk walk only catches edits made outside it (Obsidian, Explorer). Keep it
            # under ~10% of the time: 2s for small vaults, ~20s at 100k notes.
            walk = self._last_sync - started if not changed else min(self._last_sync - started, 0.5)
            self._sync_every = max(SYNC_INTERVAL, walk * 10)
        if changed or removed:
            self.semantic.kick()
        return {"notes": len(seen), "changed": changed, "removed": len(removed)}

    # ── search ───────────────────────────────────────────────────────────
    def snippet(self, path: str, q: str) -> str:
        """Cut a short excerpt around the first matching word, straight from the file."""
        try:
            with open(self.root / path, encoding="utf-8", errors="replace") as f:
                text = f.read(2_000_000)  # huge transcripts: the first 2 MB is plenty
        except OSError:
            return ""
        body = re.sub(r"\A\s*#\s[^\n]*\n", "", split_frontmatter(text)[1])  # the title is shown already
        words = [re.escape(w) for w in re.findall(r"\w+", q, flags=re.UNICODE) if len(w) > 1]
        m = re.search(r"\b(" + "|".join(words) + r")\w*", body, flags=re.I) if words else None
        if not m:
            return " ".join(MD_NOISE.sub("", body[:SNIPPET_CHARS * 2]).split())[:SNIPPET_CHARS]
        start = max(0, m.start() - SNIPPET_CHARS // 2)
        piece = body[start:start + SNIPPET_CHARS]
        piece = re.sub(r"\b(" + "|".join(words) + r")(\w*)", lambda x: f"«{x.group(0)}»", piece, flags=re.I)
        piece = " ".join(MD_NOISE.sub("", piece).split())
        return ("… " if start else "") + piece + (" …" if start + SNIPPET_CHARS < len(body) else "")

    @staticmethod
    def _scope_sql(scope: str) -> str:
        if scope == "notes":
            return " AND n.path NOT LIKE 'Conversations/%'"
        if scope == "conversations":
            return " AND n.path LIKE 'Conversations/%'"
        return ""

    def keyword_search(self, q: str, limit: int = 10, tag: str | None = None, any_word: bool = False,
                       scope: str = "all") -> list[Hit]:
        args: list = []
        if self.has_fts and q.strip():
            sql = ("SELECT n.path, n.title, bm25(fts, 8.0, 4.0, 1.0) AS score "
                   "FROM fts JOIN notes n ON n.id = fts.rowid WHERE fts MATCH ?")
            args.append(fts_query(q, any_word))
        elif q.strip():
            like = f"%{q.strip()}%"
            sql = ("SELECT n.path, n.title, 0 AS score FROM fts JOIN notes n ON n.id = fts.rowid "
                   "WHERE (fts.title LIKE ? OR fts.body LIKE ?)")
            args += [like, like]
        else:  # tag-only listing
            sql = "SELECT n.path, n.title, -n.mtime AS score FROM notes n WHERE 1"
        if tag:
            sql += " AND (' ' || n.tags || ' ') LIKE ?"
            args.append(f"% {tag.lstrip('#')} %")
        sql += self._scope_sql(scope) + " ORDER BY score LIMIT ?"
        args.append(limit)
        try:
            rows = self.db.execute(sql, args).fetchall()
        except sqlite3.OperationalError:
            return []
        return [Hit(r["path"], r["title"], "", r["score"], ["keyword"]) for r in rows]

    def search(self, q: str, limit: int = 10, tag: str | None = None, scope: str = "all") -> list[Hit]:
        """Hybrid search: keyword (BM25) fused with meaning (embeddings) when available.
        scope: all | notes (skip imported conversations) | conversations."""
        self.sync()
        if not q.strip() and not tag:
            return []
        out = self._search(q, limit, tag, scope)
        for h in out:
            if not h.snippet and q.strip():
                h.snippet = self.snippet(h.path, q)
        return out

    def _search(self, q: str, limit: int, tag: str | None, scope: str) -> list[Hit]:
        kw = self.keyword_search(q, max(limit, 20), tag, scope=scope)
        if not kw and q.strip():  # nothing matched every word: fall back to any word
            kw = self.keyword_search(q, max(limit, 20), tag, any_word=True, scope=scope)
        sem = self.semantic.search(q, max(limit, 20)) if q.strip() and scope != "conversations" else []
        if sem and scope == "notes":
            sem = [h for h in sem if not h.path.startswith("Conversations/")]
        if tag and sem:
            tagged = {h.path for h in self.keyword_search("", 10000, tag)}
            sem = [h for h in sem if h.path in tagged]
        if not sem:
            return kw[:limit]
        # Reciprocal rank fusion: robust, needs no score calibration.
        fused: dict[str, Hit] = {}
        scores: dict[str, float] = {}
        for ranked in (kw, sem):
            for rank, h in enumerate(ranked):
                scores[h.path] = scores.get(h.path, 0) + 1 / (60 + rank)
                if h.path in fused:
                    fused[h.path].via += h.via
                else:
                    fused[h.path] = h
        out = sorted(fused.values(), key=lambda h: -scores[h.path])[:limit]
        for h in out:
            h.score = round(scores[h.path], 5)
        return out

    def related(self, path: str, limit: int = 6) -> list[Hit]:
        """Notes about the same thing that are not linked yet."""
        path = self._must_find(path)
        linked = {path, *self.backlinks(path), *(l["path"] for l in self.outlinks(path) if l["path"])}
        hits = self.semantic.similar(path, limit + len(linked))
        if not hits:  # keyword fallback: title + tags as an any-word query
            row = self.db.execute("SELECT title, tags FROM notes WHERE path=?", (path,)).fetchone()
            hits = self.keyword_search(f"{row['title']} {row['tags']}", limit + len(linked), any_word=True)
        return [h for h in hits if h.path not in linked and Path(h.path).name.lower() not in SYSTEM_NOTES][:limit]

    # ── recall: the token-efficient way for an AI to pull context ────────
    def _text(self, path: str) -> str:
        """File contents with a small mtime-keyed cache (recall reads the same notes often)."""
        p = self.root / path
        try:
            mt = p.stat().st_mtime
        except OSError:
            return ""
        hit = self._text_cache.get(path)
        if hit and hit[0] == mt:
            return hit[1]
        try:
            text = p.read_text(encoding="utf-8", errors="replace")[:1_500_000]
        except OSError:
            return ""
        if len(self._text_cache) > 600:
            self._text_cache.clear()
        self._text_cache[path] = (mt, text)
        return text

    def _version_key(self) -> tuple:
        r = self.db.execute("SELECT count(*), coalesce(max(mtime),0), coalesce(sum(size),0) FROM notes").fetchone()
        return tuple(r)

    def _idf(self, terms: list[str]) -> dict[str, float]:
        import math
        n = max(1, self.db.execute("SELECT count(*) FROM notes").fetchone()[0])
        out = {}
        for t in terms:
            df = 0
            if self.has_fts:
                try:
                    df = self.db.execute("SELECT count(*) FROM fts WHERE fts MATCH ?", (f'"{t}"*',)).fetchone()[0]
                except sqlite3.OperationalError:
                    df = 0
            out[t] = math.log(1 + n / (1 + df))
        return out

    def recall(self, query: str, budget: int = 1200, hops: int = 1, scope: str = "all") -> dict:
        """Answer-ready context in one call, within a token budget.

        1. Hybrid search (keyword + meaning) picks seed notes.
        2. Chained links: each seed's links and backlinks join the pool at a decayed
           weight (hops=2 follows links of links), so connected context comes along.
        3. Every note in the pool is split into heading-labelled passages; each passage
           is scored by IDF-weighted query-term hits times its note's weight.
        4. The best passages are packed greedily into the budget (max 3 per note,
           near-duplicates dropped, long passages trimmed to their relevant sentences).
        Typical saving versus search + reading whole notes: 70-95% fewer tokens.
        """
        self.sync()
        budget = max(200, min(int(budget), 20000))
        key = (query.strip().lower(), budget, hops, scope, self._version_key())
        if key in self._recall_cache:
            return self._recall_cache[key]
        terms = [t.lower() for t in dict.fromkeys(content_words(query)) if len(t) > 1]
        if not terms:
            return {"query": query, "passages": [], "more": [], "tokens": 0, "budget": budget}
        stems = {t: t[:max(3, len(t) - 2)] if len(t) > 4 else t for t in terms}  # crude stemming: plan/plans/planning
        idf = self._idf(list(stems.values()))
        weight: dict[str, float] = {}
        via: dict[str, str] = {}
        seeds = self._search(query, 20, None, scope)
        if len(seeds) < 8:  # strict match found little: widen to any content word
            seeds += [h for h in self.keyword_search(query, 20, any_word=True, scope=scope) if h.path not in {x.path for x in seeds}]
        for rank, h in enumerate(seeds):
            # Rank matters, but passage evidence decides: a short note whose section answers
            # the question must beat a giant note that merely mentions every word.
            weight[h.path] = max(weight.get(h.path, 0), 0.65 + 0.35 / (1 + 0.5 * rank))
        frontier = [h.path for h in seeds[:6]]
        decay = 0.5
        for _ in range(max(0, min(hops, 2))):
            nxt = []
            for src in frontier:
                neighbours = self.backlinks(src) + [l["path"] for l in self.outlinks(src) if l["path"]]
                for nb in neighbours[:25]:
                    if scope == "notes" and nb.startswith("Conversations/"):
                        continue
                    w = weight[src] * decay
                    if w > weight.get(nb, 0):
                        if nb not in weight:
                            via[nb] = src
                            nxt.append(nb)
                        weight[nb] = max(weight.get(nb, 0), w)
            frontier, decay = nxt[:12], decay * 0.5
        term_re = {t: re.compile(r"\b" + re.escape(s), re.I) for t, s in stems.items()}
        passages = []
        for path, w in sorted(weight.items(), key=lambda x: -x[1])[:30]:
            meta, body = split_frontmatter(self._text(path))
            title = self.db.execute("SELECT title FROM notes WHERE path=?", (path,)).fetchone()
            title = title["title"] if title else Path(path).stem
            title_hits = sum(1 for r in term_re.values() if r.search(title))
            prev_heading = None
            for pos, (heading, chunk) in enumerate(_chunks(body)):
                last_heading = heading.rsplit(" › ", 1)[-1]
                opens_section = heading != prev_heading
                prev_heading = heading
                text_hits = {t: len(r.findall(chunk)) for t, r in term_re.items()}
                # A query word in a heading says what the section is ABOUT: it counts like several
                # mentions, but only for the passage that opens the section (not every paragraph under it).
                hits = {t: c + (HEADING_WEIGHT if opens_section and term_re[t].search(last_heading) else 0)
                        for t, c in text_hits.items()}
                matched = [t for t, c in hits.items() if c]
                if matched:
                    coverage = len(matched) / len(terms)
                    score = w * (sum(idf[stems[t]] * (1 + min(c, 8) ** 0.5) for t, c in hits.items() if c)
                                 * (0.4 + coverage) ** 1.5 + 0.6 * title_hits + (0.3 if pos == 0 else 0))
                elif pos == 0 and (path in via or (title_hits and w >= 0.5)):
                    # Chained context: a linked note contributes its opening (what it is about),
                    # ranked by link strength, so it fills leftover budget after direct matches.
                    score = w * 1.2
                else:
                    continue
                passages.append({"path": path, "title": title, "heading": heading, "text": chunk, "score": score,
                                 "pos": pos, "opens": opens_section, "via": via.get(path),
                                 "external": str(meta.get("trust", "")).lower() == "external"})
        passages.sort(key=lambda p: -p["score"])
        chosen, used, per_note, seen = [], 0, {}, set()
        for p in passages:
            if per_note.get(p["path"], 0) >= 3:
                continue
            text = _trim(p["text"], term_re, 700, keep_first=p["pos"] == 0 or p["opens"] or p["path"] in via)
            sig = re.sub(r"\W+", "", text.lower())[:160]
            if sig in seen:
                continue
            cost = (len(text) + len(p["path"]) + len(p["heading"]) + 12) // 4 + 1
            if used + cost > budget:
                if used > budget * 0.6:
                    continue
                room = (budget - used) * 4 - len(p["path"]) - 40
                if room < 200:
                    continue
                text, cost = text[:room].rsplit(" ", 1)[0] + " …", budget - used
            seen.add(sig)
            per_note[p["path"]] = per_note.get(p["path"], 0) + 1
            used += cost
            chosen.append({**p, "text": text})
        order = {p: i for i, p in enumerate(dict.fromkeys(c["path"] for c in chosen))}
        chosen.sort(key=lambda c: (order[c["path"]], c["pos"]))
        more = [p for p, _ in sorted(weight.items(), key=lambda x: -x[1]) if p not in order][:8]
        out = {"query": query, "passages": [{k: c[k] for k in ("path", "title", "heading", "text", "via", "external")} for c in chosen],
               "more": more, "tokens": used, "budget": budget, "notes": len(order)}
        if len(self._recall_cache) > 200:
            self._recall_cache.clear()
        self._recall_cache[key] = out
        return out

    # ── reading ──────────────────────────────────────────────────────────
    def read(self, path: str, section: str | None = None, max_chars: int | None = None) -> dict:
        found = self._must_find(path)
        p = self.resolve(found)
        text = p.read_text(encoding="utf-8", errors="replace")
        meta, body = split_frontmatter(text)
        content = text
        if section:
            content = extract_section(body, section)
        truncated = False
        if max_chars and len(content) > max_chars:
            content, truncated = content[:max_chars], True
        row = self.db.execute("SELECT title, tags FROM notes WHERE path=?", (found,)).fetchone()
        st = p.stat()
        return {
            "path": found,
            "title": row["title"] if row else Path(found).stem,
            "tags": (row["tags"].split() if row and row["tags"] else []),
            "meta": meta,
            "content": content,
            "truncated": truncated,
            "backlinks": self.backlinks(found),
            "links": self.outlinks(found),
            "modified": datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds"),
            "mtime": st.st_mtime,
            "versions": len(self.history(found)),
        }

    def backlinks(self, path: str) -> list[str]:
        # The names a [[link]] could use for this note (direct, no scan of every name).
        m = self._name_map()
        row = self.db.execute("SELECT title FROM notes WHERE path=?", (path,)).fetchone()
        cands = {path.lower(), path[:-3].lower(), Path(path).stem.lower()}
        if row:
            cands.add(row["title"].lower())
        names = {k for k in cands if m.get(k) == path}
        if not names:
            return []
        q = ",".join("?" * len(names))
        rows = self.db.execute(
            f"SELECT DISTINCT src FROM links WHERE dst_l IN ({q}) AND src != ? ORDER BY src", (*names, path)
        )
        return [r["src"] for r in rows]

    def outlinks(self, path: str) -> list[dict]:
        return [{"name": r["dst"], "path": self._link_target(r["dst"])}
                for r in self.db.execute("SELECT DISTINCT dst FROM links WHERE src=? ORDER BY dst", (path,))]

    def _link_target(self, name: str) -> str | None:
        m = self._name_map()
        n = name.strip().lower()
        return m.get(n) or m.get(Path(n).stem)

    def recent(self, limit: int = 20, conversations: bool | str = False) -> list[dict]:
        """conversations: False = notes only, True = everything, "only" = imported conversations only."""
        self.sync()
        where = {False: " WHERE path NOT LIKE 'Conversations/%'", True: "",
                 "only": " WHERE path LIKE 'Conversations/%'"}[conversations]
        rows = self.db.execute(f"SELECT path,title,mtime,tags FROM notes{where} ORDER BY mtime DESC LIMIT ?", (limit,))
        return [
            {"path": r["path"], "title": r["title"], "tags": r["tags"].split() if r["tags"] else [],
             "modified": datetime.fromtimestamp(r["mtime"]).isoformat(timespec="seconds")}
            for r in rows
        ]

    def tree(self) -> list[dict]:
        self.sync()
        return [dict(r) for r in self.db.execute("SELECT path,title FROM notes ORDER BY lower(path)")]

    def tags(self) -> list[dict]:
        self.sync()
        counts: dict[str, int] = {}
        for r in self.db.execute("SELECT tags FROM notes WHERE tags != ''"):
            for t in r["tags"].split():
                counts[t] = counts.get(t, 0) + 1
        return [{"tag": t, "count": c} for t, c in sorted(counts.items(), key=lambda x: (-x[1], x[0]))]

    def graph(self) -> dict:
        self.sync()
        nodes = [{"id": r["path"], "title": r["title"]} for r in self.db.execute("SELECT path,title FROM notes")]
        edges = []
        for r in self.db.execute("SELECT src,dst FROM links"):
            t = self._link_target(r["dst"])
            if t and t != r["src"]:
                edges.append({"source": r["src"], "target": t})
        return {"nodes": nodes, "edges": edges}

    def stats(self) -> dict:
        self.sync()
        r = self.db.execute("SELECT count(*) c, coalesce(sum(words),0) w FROM notes").fetchone()
        links = self.db.execute("SELECT count(*) FROM links").fetchone()[0]
        open_tasks = self.db.execute("SELECT count(*) FROM tasks WHERE done=0").fetchone()[0]
        convs = self.db.execute("SELECT count(*) FROM notes WHERE path LIKE 'Conversations/%'").fetchone()[0]
        return {"notes": r["c"] - convs, "conversations": convs, "words": r["w"], "links": links, "open_tasks": open_tasks,
                "vault": str(self.root), "fts": self.has_fts, "semantic": self.semantic.status()}

    # ── version history (undo for any change made through Cortex) ────────
    def _hist_dir(self, rel: str) -> Path:
        """History folder for a note. The path is validated like any note path, so
        '../../..' can't walk out of .cortex/history (that read files anywhere on disk)."""
        d = (self.history_dir / self.rel(self.resolve(rel))).resolve()
        if self.history_dir.resolve() not in d.parents:
            raise ValueError("bad history path")
        return d

    def _snapshot(self, p: Path, agent: str) -> None:
        if not p.exists():
            return
        d = self._hist_dir(self.rel(p))
        d.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        safe_agent = re.sub(r"[^\w-]", "", agent)[:24] or "unknown"
        shutil.copy2(p, d / f"{stamp}__{safe_agent}.md")
        versions = sorted(d.glob("*.md"))
        for old in versions[:-HISTORY_KEEP]:
            old.unlink(missing_ok=True)

    def history(self, path: str) -> list[dict]:
        rel = path if path.endswith(".md") else path + ".md"
        d = self._hist_dir(rel)
        if not d.is_dir():
            return []
        out = []
        for f in sorted(d.glob("*.md"), reverse=True):
            stamp, _, agent = f.stem.partition("__")
            try:
                ts = datetime.strptime(stamp, "%Y%m%d-%H%M%S-%f").isoformat(timespec="seconds")
            except ValueError:
                continue
            out.append({"id": f.name, "saved": ts, "replaced_by": agent, "size": f.stat().st_size})
        return out

    def version(self, path: str, vid: str) -> str:
        if not re.fullmatch(r"\d{8}-\d{6}-\d{6}__[\w-]{1,24}\.md", vid):
            raise ValueError("bad version id")
        f = self._hist_dir(path) / vid
        if not f.is_file() or f.is_symlink():
            raise FileNotFoundError(vid)
        return f.read_text(encoding="utf-8", errors="replace")

    def restore(self, path: str, vid: str, agent: str = "you") -> dict:
        content = self.version(path, vid)
        r = self.write(path, content, agent=agent, mode="overwrite", log=False)
        try:
            when = datetime.strptime(vid.split("__")[0], "%Y%m%d-%H%M%S-%f").strftime("%Y-%m-%d %H:%M")
        except ValueError:
            when = vid
        self.log(agent, "restored", r["path"], f"back to the version saved {when}")
        return {"path": r["path"], "action": "restored"}

    # ── writing ──────────────────────────────────────────────────────────
    def _atomic_write(self, p: Path, content: str) -> None:
        """Write via a temp file + rename, so Obsidian/sync never see half a note.
        The temp name is unique per process and thread: the app and an AI saving the same note
        at the same moment must not share (and clobber) one temp file."""
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(f".{p.name}.{os.getpid()}-{threading.get_ident()}.tmp")
        tmp.write_text(content if content.endswith("\n") else content + "\n", encoding="utf-8")
        for attempt in range(7):
            try:
                os.replace(tmp, p)
                return
            except PermissionError:
                # Windows: antivirus, the search indexer or a sync app briefly holds the file open.
                if attempt == 6:
                    try:
                        tmp.unlink()
                    except OSError:
                        pass
                    raise PermissionError(f"{p.name} is locked by another program (antivirus, sync or an editor); try again")
                time.sleep(0.05 * 2 ** attempt)

    def store(self, path: str, content: str) -> str:
        """Machine write for imports: atomic + indexed, but no version snapshot,
        duplicate check or per-note log line (the importer logs one summary)."""
        p = self.resolve(path)
        with self._lock:
            self._atomic_write(p, content)
            self._index_one(p, p.stat())
            self.db.commit()
        return self.rel(p)

    def similar_titles(self, path: str) -> list[str]:
        """Existing notes whose title/filename looks like this new one (duplicate guard)."""
        stem = norm_title(Path(path).stem)
        if len(stem) < 3:
            return []
        out = []
        for r in self.db.execute("SELECT path,title FROM notes"):
            for cand in (norm_title(Path(r["path"]).stem), norm_title(r["title"])):
                if cand and (cand == stem or (len(stem) >= 5 and (stem in cand or cand in stem))):
                    out.append(r["path"])
                    break
        return out[:5]

    def write(self, path: str, content: str, agent: str = "human", mode: str = "overwrite",
              expect_mtime: float | None = None, log: bool = True) -> dict:
        """mode: overwrite | append | create (fails if it exists).
        expect_mtime: refuse to overwrite if the file changed since it was read
        (stops you and an AI silently clobbering each other's edits)."""
        if mode not in ("overwrite", "append", "create"):
            raise ValueError("mode must be overwrite, append or create")
        p = self.resolve(path)
        self.check_agent_write(self.rel(p), agent)
        with self._lock:
            existed = p.exists()
            if mode == "create" and existed:
                raise FileExistsError(f"{self.rel(p)} already exists")
            if expect_mtime is not None and existed and abs(p.stat().st_mtime - expect_mtime) > 1e-3:
                raise ConflictError(f"{self.rel(p)} was changed by someone else since you opened it")
            similar = [] if existed else [s for s in self.similar_titles(self.rel(p)) if s != self.rel(p)]
            if mode == "append" and existed:
                old = p.read_text(encoding="utf-8", errors="replace")
                sep = "" if old.endswith("\n\n") else ("\n" if old.endswith("\n") else "\n\n")
                content = old + sep + content
            self._snapshot(p, agent)
            self._atomic_write(p, content)
            self._index_one(p, p.stat())
            self.db.commit()
        rel = self.rel(p)
        action = "appended" if mode == "append" and existed else ("updated" if existed else "created")
        if log:
            self.log(agent, action, rel)
        self.semantic.kick()
        out = {"path": rel, "action": action}
        if similar:
            out["similar"] = similar
        return out

    def edit(self, path: str, find: str, replace: str, agent: str = "human", all: bool = False) -> dict:
        """Exact find-and-replace inside one note (cheap, precise, no full rewrite)."""
        rel = self._must_find(path)
        p = self.resolve(rel)
        text = p.read_text(encoding="utf-8", errors="replace")
        n = text.count(find) if find else 0
        if n == 0:
            raise ValueError("the text to replace was not found; read the note and copy it exactly")
        if n > 1 and not all:
            raise ValueError(f"the text appears {n} times; include more surrounding text or set all=true")
        new = text.replace(find, replace) if all else text.replace(find, replace, 1)
        self.write(rel, new, agent=agent, log=False)
        self.log(agent, "edited", rel)
        return {"path": rel, "action": "edited", "replacements": n if all else 1}

    def write_section(self, path: str, heading: str, content: str, agent: str = "human", mode: str = "replace") -> dict:
        """Replace or append to the body of one heading; creates the heading if missing."""
        rel = self.find(path) or self.rel(self.resolve(path))
        p = self.resolve(rel)
        text = p.read_text(encoding="utf-8", errors="replace") if p.exists() else f"# {Path(rel).stem}\n"
        want = heading.lstrip("#").strip()
        matches = list(HEADING.finditer(text))
        for i, m in enumerate(matches):
            if m.group(2).strip().lower() == want.lower():
                level = len(m.group(1))
                end = len(text)
                for nxt in matches[i + 1:]:
                    if len(nxt.group(1)) <= level:
                        end = nxt.start()
                        break
                body_start = m.end() + 1 if m.end() < len(text) and text[m.end()] == "\n" else m.end()
                old_body = text[body_start:end].strip("\n")
                new_body = content.strip("\n") if mode == "replace" else (old_body + "\n" + content.strip("\n")).strip("\n")
                text = text[:body_start] + new_body + "\n\n" + text[end:].lstrip("\n")
                break
        else:
            text = text.rstrip("\n") + f"\n\n## {want}\n{content.strip()}\n"
        self.write(rel, text.rstrip("\n") + "\n", agent=agent, log=False)
        self.log(agent, "edited", rel, f"section: {want}")
        return {"path": rel, "action": "edited", "section": want}

    def move(self, src: str, dst: str, agent: str = "human") -> dict:
        """Rename/move a note and rewrite every [[link]] that pointed to it."""
        old_rel = self._must_find(src)
        old_p, new_p = self.resolve(old_rel), self.resolve(dst)
        if new_p.exists():
            raise FileExistsError(f"{self.rel(new_p)} already exists")
        new_rel = self.rel(new_p)
        self.check_agent_write(old_rel, agent)
        self.check_agent_write(new_rel, agent)
        names = {k for k, v in self._name_map().items() if v == old_rel}
        referrers = self.backlinks(old_rel)
        new_name = new_rel[:-3] if Path(new_rel).stem.lower() in {
            Path(r["path"]).stem.lower() for r in self.db.execute("SELECT path FROM notes") if r["path"] != old_rel
        } else Path(new_rel).stem  # use the full path only if the bare name would be ambiguous
        with self._lock:
            self._snapshot(old_p, agent)
            new_p.parent.mkdir(parents=True, exist_ok=True)
            os.replace(old_p, new_p)
            self._drop(old_rel)
            self._index_one(new_p, new_p.stat())
            updated = []
            for ref in referrers:
                rp = self.resolve(ref)
                text = rp.read_text(encoding="utf-8", errors="replace")

                def swap(m):
                    if m.group(2).strip().lower() not in names:
                        return m.group(0)
                    return f"{m.group(1)}[[{new_name}{m.group(3)}{m.group(4)}]]"

                new_text = WIKILINK_FULL.sub(swap, text)
                if new_text != text:
                    self._snapshot(rp, agent)
                    self._atomic_write(rp, new_text)
                    self._index_one(rp, rp.stat())
                    updated.append(ref)
            self.db.commit()
        hist_old, hist_new = self._hist_dir(old_rel), self._hist_dir(new_rel)
        if hist_old.is_dir() and not hist_new.exists():
            hist_new.parent.mkdir(parents=True, exist_ok=True)
            os.replace(hist_old, hist_new)
        self.log(agent, "moved", new_rel, f"from {old_rel}; fixed links in {len(updated)} note(s)")
        self.semantic.kick()
        return {"path": new_rel, "from": old_rel, "action": "moved", "links_updated": updated}

    def delete(self, path: str, agent: str = "human") -> dict:
        p = self.resolve(path)
        if not p.exists():
            raise FileNotFoundError(path)
        self._snapshot(p, agent)
        trash = self.root / ".trash"
        trash.mkdir(exist_ok=True)
        dest = trash / f"{int(time.time())}-{p.name}"
        os.replace(p, dest)  # recoverable, like Obsidian's own .trash
        rel = self.rel(p)
        with self._lock:
            self._drop(rel)
            self.db.commit()
        self.log(agent, "deleted", rel)
        return {"path": rel, "action": "deleted"}

    def daily_path(self, day: date | None = None) -> str:
        return f"Daily/{(day or date.today()).isoformat()}.md"

    def capture(self, text: str, agent: str = "human") -> dict:
        """Quick capture: append a timestamped line to today's daily note."""
        path = self.daily_path()
        stamp = datetime.now().strftime("%H:%M")
        line = f"- {stamp} {text.strip()}" + (f" _(via {agent})_" if agent not in ("human", "you") else "")
        if not self.resolve(path).exists():
            day = date.today().isoformat()
            self.write(path, f"# {day}\n\n## Log\n\n{line}", agent=agent, mode="create")
            return {"path": path, "action": "created"}
        return self.write(path, line, agent=agent, mode="append")

    # ── tasks (every "- [ ]" checkbox in the vault) ──────────────────────
    def tasks(self, status: str = "open", path: str | None = None, query: str | None = None, limit: int = 200) -> list[dict]:
        self.sync()
        sql, args = "SELECT t.path, t.line, t.text, t.done, t.due, n.title FROM tasks t JOIN notes n ON n.path=t.path WHERE 1", []
        if status == "open":
            sql += " AND t.done=0"
        elif status == "done":
            sql += " AND t.done=1"
        if path:
            sql += " AND t.path=?"
            args.append(self._must_find(path))
        if query:
            sql += " AND t.text LIKE ?"
            args.append(f"%{query}%")
        sql += " ORDER BY t.done, t.due IS NULL, t.due, n.mtime DESC, t.line LIMIT ?"
        args.append(limit)
        today = date.today().isoformat()
        return [{"path": r["path"], "title": r["title"], "line": r["line"], "text": r["text"], "done": bool(r["done"]),
                 "due": r["due"], "overdue": bool(r["due"] and r["due"] < today and not r["done"])}
                for r in self.db.execute(sql, args)]

    def add_task(self, text: str, path: str | None = None, due: str | None = None, agent: str = "human") -> dict:
        text = text.strip().replace("\n", " ")
        if due:
            datetime.strptime(due, "%Y-%m-%d")  # validates
            text += f" 📅 {due}"
        if path:
            r = self.write_section(path, "Tasks", f"- [ ] {text}", agent=agent, mode="append")
        else:
            target = self.daily_path()
            if not self.resolve(target).exists():
                self.capture("Started the day", agent=agent)
            r = self.write_section(target, "Tasks", f"- [ ] {text}", agent=agent, mode="append")
        return {"path": r["path"], "action": "task added", "task": text}

    def toggle_task(self, path: str, line: int, text: str | None = None, agent: str = "human", done: bool | None = None) -> dict:
        rel = self._must_find(path)
        p = self.resolve(rel)
        lines = p.read_text(encoding="utf-8", errors="replace").split("\n")
        idx = line
        def ok(i):
            m = 0 <= i < len(lines) and TASK.match(lines[i])
            return m and (text is None or m.group(4).strip() == text.strip())
        if not ok(idx):  # the note changed since it was listed: find the task by its text
            idx = next((i for i in range(len(lines)) if ok(i)), -1)
            if idx < 0:
                raise ValueError("task not found; the note may have changed")
        m = TASK.match(lines[idx])
        now_done = (m.group(2) == " ") if done is None else done
        lines[idx] = f"{m.group(1)}{'x' if now_done else ' '}{m.group(3)}{m.group(4)}"
        self.write(rel, "\n".join(lines), agent=agent, log=False)
        self.log(agent, "completed task" if now_done else "reopened task", rel, m.group(4).strip()[:120])
        return {"path": rel, "line": idx, "done": now_done}

    # ── health check ─────────────────────────────────────────────────────
    def health(self, stale_days: int = 60) -> dict:
        self.sync(force=True)
        notes = {r["path"]: dict(r) for r in self.db.execute("SELECT path,title,mtime,words,status FROM notes")}
        name_map = self._name_map()
        linked_to = set()
        broken = []
        for r in self.db.execute("SELECT src,dst FROM links"):
            t = name_map.get(r["dst"].strip().lower()) or name_map.get(Path(r["dst"].strip().lower()).stem)
            if t:
                linked_to.add(t)
            else:
                broken.append({"note": r["src"], "link": r["dst"]})
        has_out = {r["src"] for r in self.db.execute("SELECT DISTINCT src FROM links")}
        orphans = [p for p in notes if p not in linked_to and p not in has_out
                   and not p.startswith("Daily/") and Path(p).name.lower() not in SYSTEM_NOTES]
        empty = [p for p, n in notes.items() if n["words"] < 5 and Path(p).name.lower() not in SYSTEM_NOTES]
        by_title: dict[str, list[str]] = {}
        for p, n in notes.items():
            by_title.setdefault(norm_title(n["title"]), []).append(p)
        by_stem: dict[str, list[str]] = {}
        for p in notes:
            by_stem.setdefault(Path(p).stem.lower(), []).append(p)
        dupes = {tuple(sorted(v)) for v in list(by_title.values()) + list(by_stem.values()) if len(v) > 1}
        cutoff = time.time() - stale_days * 86400
        stale = [{"note": p, "status": n["status"], "days": int((time.time() - n["mtime"]) / 86400)}
                 for p, n in notes.items() if n["status"] in ACTIVE_STATUSES and n["mtime"] < cutoff]
        big = [{"note": p, "words": n["words"]} for p, n in notes.items() if n["words"] > 4000]
        overdue = [t for t in self.tasks("open") if t["overdue"]]
        from .cloud import conflict_copies
        conflicts = conflict_copies(list(notes))
        conflict_paths = {c["note"] for c in conflicts}
        dupes = {d for d in dupes if not (set(d) & conflict_paths)}  # reported once, as a conflict
        issues = len(broken) + len(dupes) + len(stale) + len(overdue) + len(empty) + 2 * len(conflicts)
        score = max(0, 100 - min(60, issues * 3) - min(20, len(orphans)))
        return {
            "score": score,
            "notes": len(notes),
            "sync_conflicts": conflicts,
            "broken_links": broken,
            "duplicates": [list(d) for d in sorted(dupes)],
            "stale": sorted(stale, key=lambda s: -s["days"]),
            "overdue_tasks": overdue,
            "empty": sorted(empty),
            "orphans": sorted(orphans),
            "too_long": big,
        }

    # ── recap (raw material for daily/weekly reviews) ────────────────────
    def recap(self, days: int = 7) -> dict:
        self.sync()
        since = time.time() - days * 86400
        created, changed = [], []
        for r in self.db.execute("SELECT path,title,mtime,created FROM notes WHERE mtime>=? ORDER BY mtime DESC", (since,)):
            (created if (r["created"] or 0) >= since else changed).append({"path": r["path"], "title": r["title"]})
        acts = [a for a in self.activity(1000) if _ts_key(a["ts"]) >= since]
        by_agent: dict[str, int] = {}
        for a in acts:
            by_agent[a["agent"]] = by_agent.get(a["agent"], 0) + 1
        done = [a for a in acts if a["action"] == "completed task"]
        return {"days": days, "created": created, "changed": changed, "by_agent": by_agent,
                "completed_tasks": [{"note": a["target"], "task": a.get("note", "")} for a in done],
                "decisions_and_work": [a for a in acts if a.get("note") and a["action"] in ("did", "decided")],
                "open_tasks": self.tasks("open", limit=30)}

    # ── shared activity log (one logbook for every AI, on every device) ──
    # Each device appends only to its own file, so cloud sync (Google Drive etc.) never
    # sees two machines writing the same file; reads merge all devices by time.
    def log(self, agent: str, action: str, target: str = "", note: str = "") -> None:
        entry = {"ts": datetime.now(timezone.utc).astimezone().isoformat(timespec="microseconds"),
                 "agent": agent, "action": action, "target": target, "device": DEVICE}
        if note:
            entry["note"] = note
        self.activity_dir.mkdir(exist_ok=True)
        with self._lock, open(self.activity_dir / f"{DEVICE}.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    @staticmethod
    def _tail(path: Path, lines: int) -> list[dict]:
        """Last `lines` entries of one log file, read from the end (fast on long logs)."""
        try:
            with open(path, "rb") as f:
                f.seek(0, os.SEEK_END)
                size, chunk, data = f.tell(), 65536, b""
                while size > 0 and data.count(b"\n") <= lines:
                    step = min(chunk, size)
                    size -= step
                    f.seek(size)
                    data = f.read(step) + data
        except OSError:
            return []
        out = []
        for line in data.decode("utf-8", errors="replace").splitlines()[-lines:]:
            try:
                out.append(json.loads(line))
            except ValueError:
                continue  # a half-synced last line from another device: skip it
        return out

    def activity(self, limit: int = 50, agent: str | None = None) -> list[dict]:
        files = list(self.activity_dir.glob("*.jsonl")) if self.activity_dir.is_dir() else []
        legacy = self.meta_dir / "activity.jsonl"  # single-file log from v1.0-1.3
        if legacy.exists():
            files.append(legacy)
        want = limit * (6 if agent else 2)
        keyed = [(_ts_key(e["ts"]), i, e) for f in files for i, e in enumerate(self._tail(f, want))
                 if isinstance(e, dict) and "ts" in e and (not agent or e.get("agent") == agent)]
        keyed.sort(key=lambda x: (x[0], x[1]), reverse=True)  # real time (devices may differ in zone), then file order
        return [e for _, _, e in keyed[:limit]]

    def close(self) -> None:
        self.semantic.stop()
        self.db.close()


def _chunks(body: str, target: int = 650):
    """Split a note into (heading path, passage) pieces of roughly `target` characters."""
    heading_stack: list[tuple[int, str]] = []
    buf: list[str] = []
    size = 0
    in_code = False

    def label():
        return " › ".join(h for _, h in heading_stack)

    for line in body.split("\n"):
        if line.lstrip().startswith("```"):
            in_code = not in_code
        m = None if in_code else HEADING.match(line)
        if m:
            if buf and "".join(buf).strip():
                yield label(), "\n".join(buf).strip()
            buf, size = [], 0
            level = len(m.group(1))
            heading_stack = [(l, h) for l, h in heading_stack if l < level] + [(level, m.group(2).strip())]
            continue
        buf.append(line)
        size += len(line) + 1
        if size >= target and not in_code and (not line.strip() or size >= target * 2):
            if "".join(buf).strip():
                yield label(), "\n".join(buf).strip()
            buf, size = [], 0
    if buf and "".join(buf).strip():
        yield label(), "\n".join(buf).strip()


def _trim(text: str, term_re: dict, limit: int, keep_first: bool = True) -> str:
    """Keep a passage compact: only the paragraphs that mention the query (plus the opening
    paragraph, which says what the passage is about); if still long, only matching sentences."""
    paras = [p.strip() for p in re.split(r"\n\s*\n|\n(?=[-*] |\d+\. )", text) if p.strip()]
    if len(paras) > 1:
        kept = [p for i, p in enumerate(paras) if (i == 0 and keep_first) or any(r.search(p) for r in term_re.values())]
        text = "\n".join(kept or paras[:1])
    text = re.sub(r"[ \t]+", " ", re.sub(r"\n{2,}", "\n", text)).strip()
    if len(text) <= limit:
        return text
    sentences = [x for x in re.split(r"(?<=[.!?])\s+|\n", text) if x.strip()]
    # The opening states what the passage is about; keep it, then the sentences that match.
    keep = sentences[:2] + [s for s in sentences[2:] if any(r.search(s) for r in term_re.values())]
    out = " ".join(s.strip() for s in keep if s.strip())
    return out if len(out) <= limit else out[:limit].rsplit(" ", 1)[0] + " …"


class ConflictError(Exception):
    """The note changed on disk after it was opened for editing."""


def extract_section(body: str, heading: str) -> str:
    """Return one heading's section (up to the next heading of same/higher level)."""
    want = heading.lstrip("#").strip().lower()
    matches = list(HEADING.finditer(body))
    for i, m in enumerate(matches):
        if m.group(2).strip().lower() == want:
            level = len(m.group(1))
            end = len(body)
            for n in matches[i + 1:]:
                if len(n.group(1)) <= level:
                    end = n.start()
                    break
            return body[m.start():end].strip()
    raise KeyError(f"no section {heading!r}")
