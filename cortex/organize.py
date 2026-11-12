"""Self-organizing memory: Cortex files, sorts and connects what it is given. No AI involved.

Everything here is deterministic and local: rules you write, keyword lists (English + Malay),
similarity to notes you already have, a date parser and plain text statistics. So it is fast
(milliseconds), predictable, and every change it makes is an ordinary change-set you can undo.

- categorize: rules (.cortex/rules.json) -> keyword lexicon -> what similar notes are filed as
- find_tasks: action items in any text ("need to", "remind me", "jangan lupa", "kena"...) + due dates
- find_entities / autolink: mentions of notes you already have become [[links]]
- near_duplicate: don't store the same thing twice
- Organizer.ingest: one call that stores, classifies, extracts tasks, links, and reports what it did
- Organizer.group_conversations: imported AI chats grouped into Topics/ notes with summaries
"""

from __future__ import annotations

import json
import math
import os
import re
from collections import Counter
from datetime import date, datetime
from pathlib import Path

from .dates import find_date, strip_date

# ── categories ───────────────────────────────────────────────────────────
LEXICON: dict[str, set[str]] = {
    "Work": set("""meeting meetings client clients deadline deadlines project projects proposal stakeholder
        office boss manager colleague colleagues standup sprint kpi okr quarterly presentation report reports
        kerja mesyuarat pelanggan bos pejabat projek laporan syarikat company""".split()),
    "Finance": set("""budget budgets invoice invoices payment payments salary bank loan tax taxes expense expenses
        bill bills rent price cost costs money savings invest investment investing stocks crypto refund
        bajet bayar bayaran gaji duit wang cukai pinjaman simpanan bil harga kos""".split()),
    "Health": set("""doctor clinic hospital gym workout exercise run running sleep diet medicine medication
        symptoms appointment dentist therapy weight calories protein health
        doktor klinik hospital ubat senaman tidur sihat kesihatan diet""".split()),
    "Learning": set("""course courses learn learning tutorial lecture study studying exam exams book books
        reading chapter notes certificate class lesson university school
        belajar kursus kelas buku bab peperiksaan universiti sekolah ulangkaji""".split()),
    "Travel": set("""flight flights hotel hotels trip trips itinerary visa passport airport booking holiday
        vacation tour train luggage
        penerbangan percutian cuti tiket lapangan pasport hotel""".split()),
    "Tech": set("""code coding bug bugs deploy deployment api server database python javascript typescript
        github git docker linux windows app frontend backend sql electron framework library compile build
        repo repository commit release""".split()),
    "Content": set("""post posts youtube tiktok instagram twitter thread threads video videos content
        caption script channel audience followers views upload podcast newsletter
        hantaran kandungan""".split()),
    "Home": set("""house home rent landlord repair repairs furniture kitchen cleaning groceries laundry
        garden utilities electricity water internet
        rumah sewa tuan baiki perabot dapur barang dapur elektrik air""".split()),
    "Family": set("""family mum mom dad mother father wife husband kids son daughter brother sister
        birthday wedding anniversary parents
        keluarga mak ibu ayah abah isteri suami anak adik abang kakak hari jadi kahwin""".split()),
    "Shopping": set("""buy order orders shopping cart delivery shopee lazada amazon purchase
        beli belian pesanan penghantaran""".split()),
    "Ideas": set("""idea ideas brainstorm concept maybe someday wishlist
        idea cadangan ilham""".split()),
}
TYPE_PATTERNS = {
    "decision": re.compile(r"\b(?:we|i)\s+(?:decided|chose|picked|agreed|settled\s+on|are\s+going\s+with|will\s+go\s+with)\b"
                           r"|\bdecision\s*:|\blet'?s\s+go\s+with\b|\b(?:kita|saya|aku|kami)\s+(?:pilih|setuju|putuskan|decide)\b"
                           r"|\bkeputusan\s*:", re.I),
    "lesson": re.compile(r"\blessons?(?:\s+learn(?:ed|t))?\s*:|\b(?:i|we)\s+learn(?:ed|t)\b|\bnext\s+time\b|\btakeaway\s*:"
                         r"|\bpengajaran\s*:|\blain\s+kali\b", re.I),
    "idea": re.compile(r"\bidea\s*:|\bwhat\s+if\b|\bmaybe\s+we\s+could\b|\bcadangan\s*:|\bapa\s+kata\b", re.I),
}
CHAT_NOISE = set("""you claude chatgpt gemini codex cursor opus sonnet haiku gpt bash tool tools assistant user
    please thanks thank okay sure let file files run command output read edit write""".split())
SECTION_FOR = {"decision": "Decisions", "lesson": "Lessons", "idea": "Ideas", "fact": "Notes", "note": "Notes"}

# ── tasks ────────────────────────────────────────────────────────────────
TRIGGERS = re.compile(
    r"^(?:.*?\b)?(?:"
    r"(?:to-?do|to\s+do)\s*[:\-]"
    r"|(?:i|we)\s+(?:really\s+)?(?:need|have|got|must|should)\s+to"
    r"|(?:i|we)\s+must"
    r"|needs?\s+to"
    r"|remember\s+to|don'?t\s+forget(?:\s+to)?|remind\s+me(?:\s+to)?|make\s+sure(?:\s+to)?"
    r"|jangan\s+lupa(?:\s+nak)?|ingatkan\s+(?:saya|aku)(?:\s+untuk)?|nak\s+kena|kena|perlu|mesti|wajib"
    r")\s+", re.I)
VERBS = set("""call email text message buy pay send book finish submit fix review write prepare schedule renew
    cancel check reply update ship clean order collect pick visit meet print sign file draft read plan
    bayar beli hantar telefon siapkan baiki tempah semak balas jumpa ambil cuci""".split())
BULLET = re.compile(r"^\s*(?:[-*+]\s+(?!\[)|\d+[.)]\s+)?(?:\d{1,2}:\d{2}\s+)?")
FIELD = re.compile(r"\s*\[[\w-]+::[^\]]*\]")
VIA = re.compile(r"\s*_\(via [^)]*\)_")

WORD = re.compile(r"[^\W\d_][\w'’-]*", re.UNICODE)


def _plain(line: str) -> str:
    return VIA.sub("", FIELD.sub("", BULLET.sub("", line))).strip()


def find_tasks(text: str, today: date | None = None) -> list[dict]:
    """Action items in free text, with due dates. Questions and existing checkboxes are skipped."""
    today = today or date.today()
    out, in_code = [], False
    for raw in text.splitlines():
        if raw.lstrip().startswith("```"):
            in_code = not in_code
        if in_code or not raw.strip() or raw.lstrip().startswith(("#", ">", "|")) or re.match(r"^\s*[-*+]\s+\[[ xX]\]", raw):
            continue
        for sentence in re.split(r"(?<=[.!?])\s+", _plain(raw)):
            s = sentence.strip()
            if len(s) < 4 or s.endswith("?"):
                continue
            m = TRIGGERS.match(s)
            due, phrase = find_date(s, today)
            if m:
                body = s[m.end():]
            elif due and (WORD.findall(s) or [""])[0].lower() in VERBS:
                body = s  # imperative with a date: "pay rent by Friday"
            else:
                continue
            body = strip_date(body, phrase).rstrip(" .!;,")
            body = re.sub(r"^(?:to|untuk|nak)\s+", "", body, flags=re.I)
            if len(WORD.findall(body)) < 1 or len(body) < 3:
                continue
            body = body[:160]
            out.append({"text": body[0].upper() + body[1:], "due": due.isoformat() if due else None})
    seen, uniq = set(), []
    for t in out:
        k = _norm(t["text"])
        if k not in seen:
            seen.add(k)
            uniq.append(t)
    return uniq


def detect_type(text: str) -> str:
    for kind in ("decision", "lesson", "idea"):
        if TYPE_PATTERNS[kind].search(text):
            return kind
    first = text.strip().splitlines()[0] if text.strip() else ""
    if TRIGGERS.match(_plain(first)) and len(text) < 300:
        return "task"
    return "note"


def _norm(s: str) -> str:
    return " ".join(w.lower() for w in WORD.findall(s))


def _words(s: str) -> list[str]:
    from .core import STOPWORDS
    return [w for w in (x.lower().strip("'’-") for x in WORD.findall(s)) if len(w) > 2 and w not in STOPWORDS]


def lexicon_category(title: str, text: str) -> tuple[str | None, float]:
    """Best lexicon category for a text and a 0-1 confidence; title words count triple."""
    counts = Counter(_words(text[:6000]))
    for w in _words(title):
        counts[w] += 3
    scores = {cat: sum(c for w, c in counts.items() if w in words) for cat, words in LEXICON.items()}
    best = max(scores, key=scores.get)
    total = sum(scores.values())
    if scores[best] < 2:
        return None, 0.0
    return best, round(min(1.0, scores[best] / max(total, 1) * min(1.0, scores[best] / 4)), 2)


def load_rules(vault: Path) -> list[dict]:
    """User rules from .cortex/rules.json: [{match: {folder, keywords, regex}, category, folder, tags}]."""
    f = vault / ".cortex" / "rules.json"
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    rules = data.get("rules", data) if isinstance(data, dict) else data
    return [r for r in rules if isinstance(r, dict) and isinstance(r.get("match"), dict)] if isinstance(rules, list) else []


def rule_category(rules: list[dict], path: str, title: str, text: str) -> dict | None:
    hay = f"{title}\n{text[:20000]}".lower()
    for r in rules:
        m = r["match"]
        if m.get("folder") and not path.lower().startswith(str(m["folder"]).lower().strip("/") + "/"):
            continue
        kws = m.get("keywords") or []
        if isinstance(kws, str):
            kws = [kws]
        if kws and not any(re.search(r"\b" + re.escape(str(k).lower()) + r"\b", hay) for k in kws):
            continue
        if m.get("regex"):
            try:
                if not re.search(str(m["regex"])[:300], hay, re.I):
                    continue
            except re.error:
                continue
        if not (m.get("folder") or kws or m.get("regex")):
            continue
        return r
    return None


def categorize(brain, title: str, text: str, path: str = "") -> dict:
    """Rules first, then the lexicon, then how the most similar existing notes are filed."""
    rule = rule_category(brain.rules(), path, title, text)
    if rule and rule.get("category"):
        return {"category": str(rule["category"]), "confidence": 1.0, "why": "rule",
                "folder": rule.get("folder"), "tags": list(rule.get("tags") or [])}
    cat, conf = lexicon_category(title, text)
    folder, votes = None, Counter()
    words = [w for w, _ in Counter(_words(f"{title} {text[:3000]}")).most_common(8)]
    if words:
        hits = brain.keyword_search(" ".join(words), 8, any_word=True, scope="notes")
        rows = {r["path"]: r["category"] for r in brain.db.execute(
            f"SELECT path, category FROM notes WHERE path IN ({','.join('?' * len(hits))})", [h.path for h in hits])} if hits else {}
        folders = Counter(h.path.split("/", 1)[0] for h in hits if "/" in h.path and not h.path.startswith(("Daily/", "Conversations/")))
        if folders:
            f, n = folders.most_common(1)[0]
            folder = f if n >= 2 else None
        votes = Counter(c for c in rows.values() if c)
    if votes:
        vcat, n = votes.most_common(1)[0]
        if n >= 3 and (not cat or conf < 0.35):
            return {"category": vcat, "confidence": round(n / 8, 2), "why": "similar notes", "folder": folder, "tags": []}
    if cat:
        return {"category": cat, "confidence": conf, "why": "keywords", "folder": folder, "tags": []}
    return {"category": None, "confidence": 0.0, "why": "", "folder": folder, "tags": []}


# ── entities and links ───────────────────────────────────────────────────
def find_entities(brain, text: str) -> list[dict]:
    """Notes you already have that this text mentions (by title or file name), longest first."""
    names = brain._name_map()
    clean = re.sub(r"```.*?```|`[^`]*`|\[\[[^\]]*\]\]|https?://\S+", " ", text, flags=re.S)
    tokens = [(m.group(0), m.start()) for m in re.finditer(r"[\w'’&+.-]+", clean, flags=re.UNICODE)]
    found: dict[str, dict] = {}
    taken = set()
    from .core import STOPWORDS
    for n in (4, 3, 2, 1):
        for i in range(len(tokens) - n + 1):
            if any(j in taken for j in range(i, i + n)):
                continue
            words = [t[0].strip(".") for t in tokens[i:i + n]]
            phrase = " ".join(words)
            key = phrase.lower()
            path = names.get(key)
            if not path or path.startswith(("Conversations/", "Daily/")) or len(key) < 3:
                continue
            if Path(path).name.lower() in ("_cortex.md", "home.md"):
                continue
            if n == 1 and (key in STOPWORDS or not words[0][:1].isupper() or len(key) < 4):
                continue  # single words only when written as a Name
            if path not in found:
                found[path] = {"name": phrase, "path": path}
            taken.update(range(i, i + n))
    return list(found.values())


def autolink(brain, text: str, entities: list[dict]) -> str:
    """Turn the first plain mention of each known note into a [[link]] (never inside code or links)."""
    for e in entities:
        stem = Path(e["path"]).stem
        target = stem if brain._link_target(stem) == e["path"] else e["path"][:-3]
        rx = re.compile(r"(?<![\w\[])(" + re.escape(e["name"]) + r")(?![\w\]])", re.I)
        protected = [(m.start(), m.end()) for m in re.finditer(r"```.*?```|`[^`]*`|\[\[[^\]]*\]\]|https?://\S+", text, flags=re.S)]
        for m in rx.finditer(text):
            if any(a <= m.start() < b for a, b in protected):
                continue
            shown = m.group(1)
            link = f"[[{shown}]]" if shown.lower() == target.lower() else f"[[{target}|{shown}]]"
            text = text[:m.start()] + link + text[m.end():]
            break
    return text


def near_duplicate(brain, text: str) -> dict | None:
    """An existing line or note that says the same thing (so it isn't stored twice)."""
    words = _words(text)
    if len(words) < 2:
        return None
    short = len(words) <= 40
    mine = set(words)
    shingles = {tuple(words[i:i + 3]) for i in range(len(words) - 2)}
    query = " ".join(w for w, _ in Counter(words).most_common(8))
    for h in brain.keyword_search(query, 6, any_word=True):
        body = brain._text(h.path)
        if short:
            for line in body.splitlines():
                other = set(_words(_plain(line)))
                if other and len(mine & other) / len(mine | other) >= 0.8:
                    return {"path": h.path, "line": _plain(line)[:200], "similarity": round(len(mine & other) / len(mine | other), 2)}
        else:
            ow = _words(body)
            theirs = {tuple(ow[i:i + 3]) for i in range(len(ow) - 2)}
            if shingles and theirs:
                sim = len(shingles & theirs) / len(shingles | theirs)
                if sim >= 0.6:
                    return {"path": h.path, "similarity": round(sim, 2)}
    return None


def title_from(text: str, limit: int = 70) -> str:
    for line in text.strip().splitlines():
        line = re.sub(r"^#+\s*", "", _plain(line)).strip()
        if line:
            first = re.split(r"(?<=[.!?])\s", line)[0]
            return first[:limit].rsplit(" ", 1)[0] if len(first) > limit else first
    return "Untitled"


# ── the organizer ────────────────────────────────────────────────────────
class Organizer:
    def __init__(self, brain):
        self.brain = brain

    def _record_line(self, kind: str, text: str, cat: str | None, source: str, agent: str, stamp: str) -> str:
        fields = f"[type:: {kind}]" + (f" [category:: {cat}]" if cat else "")
        if source and source != "user":
            fields += f" [source:: {source}]"
        if agent not in ("you", "human"):
            fields += f" [by:: {agent}]"
        return f"- {stamp} {' '.join(text.split())} {fields}"

    def _unique(self, folder: str, title: str) -> str:
        from .importers import _safe
        base = f"{folder.strip('/')}/{_safe(title, 70)}" if folder else _safe(title, 70)
        rel, n = base + ".md", 2
        while self.brain.resolve(rel).exists():
            rel, n = f"{base} {n}.md", n + 1
        return rel

    def ingest(self, text: str, agent: str = "human", source: str = "user", kind: str | None = None,
               title: str | None = None, project: str | None = None, today: date | None = None,
               dedupe: bool = True) -> dict:
        """Store anything in the right place, in one call: a fact, decision, lesson, idea, task,
        a long note or a whole conversation. Returns what Cortex did, so the caller can say it
        (or undo it with the change-set id)."""
        b = self.brain
        text = (text or "").strip()
        if not text:
            raise ValueError("nothing to store")
        if len(text) > 400_000:
            raise ValueError("text too long (max 400,000 characters)")
        source = re.sub(r"[^\w.-]", "", (source or "user").lower())[:24] or "user"
        today = today or date.today()
        b.sync()
        if dedupe and kind != "conversation":
            dup = near_duplicate(b, text)
            if dup:
                return {"action": "duplicate", "duplicate_of": dup["path"], "similarity": dup["similarity"],
                        "path": dup["path"], "tasks": [], "links": [], "changeset": None}
        kind = (kind or detect_type(text)).lower()
        if kind not in ("note", "fact", "decision", "lesson", "idea", "task", "conversation"):
            raise ValueError("type must be note, fact, decision, lesson, idea, task or conversation")
        if kind == "conversation":
            return self._ingest_conversation(text, agent, source, title, today)
        cat = categorize(b, title or "", text)
        found = find_tasks(text, today)
        open_now = {_norm(t["text"].split(" 📅")[0]): t["path"] for t in b.tasks("open", limit=2000)}
        tasks = [t for t in found if _norm(t["text"]) not in open_now]
        if kind == "task" and found and not tasks:  # every task in it is already on the list
            path = open_now[_norm(found[0]["text"])]
            return {"action": "duplicate", "duplicate_of": path, "similarity": 1.0, "path": path,
                    "tasks": [], "links": [], "changeset": None}
        ents = find_entities(b, text)
        dest = None
        if project:
            dest = b.find(project) or b.rel(b.resolve(project if "/" in project else f"Projects/{project}"))
        else:
            dest = next((e["path"] for e in ents if e["path"].startswith("Projects/")), None)
        linked = autolink(b, text, [e for e in ents if e["path"] != dest])
        short = len(text) <= 500 and text.count("\n") <= 3
        auto_file = bool(b.organize_config().get("auto_file"))
        with b.changeset(agent, f"ingest {kind}") as cs:
            if short:
                if kind == "task" and tasks:
                    target = dest
                    path = dest or b.daily_path(today)
                else:
                    target = dest or b.daily_path(today)
                    stamp = today.isoformat() if dest else datetime.now().strftime("%H:%M")
                    section = SECTION_FOR.get(kind, "Notes") if dest else "Log"
                    if not dest and not b.resolve(target).exists():
                        b.write(target, f"# {today.isoformat()}\n\n## Log\n", agent=agent, mode="create", log=False)
                    b.write_section(target, section, self._record_line(kind, linked, cat["category"], source, agent, stamp),
                                    agent=agent, mode="append")
                    path = target
            else:
                folder = (cat.get("folder") if auto_file and cat.get("folder") else None) or "Inbox"
                if dest:
                    folder = str(Path(dest).parent).replace("\\", "/")
                    folder = "" if folder == "." else folder
                name = title or title_from(text)
                path = self._unique(folder, name)
                fm = ["---", f"type: {kind}", f"created: {today.isoformat()}"]
                if cat["category"]:
                    fm.append(f"category: {cat['category']}")
                if cat.get("tags"):
                    fm.append(f"tags: [{', '.join(cat['tags'])}]")
                if source != "user":
                    fm.append(f"source: {source}")
                    if source == "web":
                        fm.append("trust: external")
                if agent not in ("you", "human"):
                    fm.append(f"by: {agent}")
                fm.append("---")
                heading = "" if re.match(r"^#\s", text) else f"# {name}\n\n"
                body = linked + (f"\n\nRelated: [[{Path(dest).stem}]]" if dest else "")
                b.write(path, "\n".join(fm) + "\n" + heading + body, agent=agent, mode="create")
                target = path
            added = []
            for t in tasks:
                r = b.add_task(t["text"], target, t["due"], agent=agent)
                added.append({"text": t["text"], "due": t["due"], "path": r["path"]})
            stubs = self._stubs(text, agent)
        return {"action": "stored", "path": path, "type": kind, "category": cat["category"],
                "confidence": cat["confidence"], "why": cat["why"], "folder_suggestion": cat.get("folder"),
                "tasks": added, "links": [e["path"] for e in ents if e["path"] != dest], "project": dest,
                "stubs": stubs, "duplicate_of": None, "changeset": cs["id"] if cs["files"] else None}

    def _ingest_conversation(self, text: str, agent: str, source: str, title: str | None, today: date) -> dict:
        b = self.brain
        from .importers import _safe
        name = title or title_from(re.sub(r"^\*\*\w+\*\*.*$", "", text, flags=re.M)) or "Conversation"
        app = (agent.split("/")[0] if agent not in ("you", "human") else source).upper() if len(agent) <= 4 else agent.split("/")[0].title()
        rel = f"Conversations/{_safe(app, 30)}/{today.isoformat()[:7]}/{today.isoformat()} {datetime.now().strftime('%H%M')} {_safe(name, 60)}.md"
        n = 2
        while b.resolve(rel).exists():
            rel = rel[:-3] + f" {n}.md"
            n += 1
        content = (f"---\ntags: [conversation]\nsource: {app}\nstarted: {datetime.now().isoformat(timespec='seconds')}\n---\n"
                   f"# {name}\n\n{text}\n")
        with b.changeset(agent, "ingest conversation") as cs:
            p = b.resolve(rel)
            stored = b.store(rel, content)  # Conversations/ is history: written once, never edited by an AI
            b._record(stored, None, p)
            b.log(agent, "created", stored)
            topics = self.group_conversations(agent="cortex", only=[stored])
        return {"action": "stored", "path": stored, "type": "conversation", "category": None, "tasks": [],
                "links": [], "topics": topics.get("touched", []), "duplicate_of": None, "changeset": cs["id"]}

    def _stubs(self, text: str, agent: str) -> list[str]:
        """Names seen in 3+ separate captures get a stub note, so they can be linked and grow."""
        cfg = self.brain.organize_config()
        if cfg.get("stubs") is False:
            return []
        b = self.brain
        cands = set()
        for m in re.finditer(r"(?<![.!?]\s)(?<!^)\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,2})\b", text):
            name = m.group(1)
            if not b._link_target(name):
                cands.add(name)
        if not cands:
            return []
        state_f = b.local_dir / "mentions.json"
        try:
            state = json.loads(state_f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            state = {}
        made = []
        for name in sorted(cands):
            state[name] = state.get(name, 0) + 1
            if state[name] == int(cfg.get("stub_after", 3)):
                rel = self._unique("Entities", name)
                b.write(rel, f"---\ntype: entity\n---\n# {name}\n\nMentioned in several notes. Cortex made this stub so they can link here.",
                        agent=agent, mode="create")
                made.append(rel)
        if len(state) > 5000:
            state = dict(sorted(state.items(), key=lambda x: -x[1])[:3000])
        tmp = state_f.with_suffix(".tmp")
        tmp.write_text(json.dumps(state), encoding="utf-8")
        os.replace(tmp, state_f)
        return made

    # ── conversation topics ──────────────────────────────────────────────
    def _topics_file(self) -> Path:
        return self.brain.meta_dir / "topics.json"

    def load_topics(self) -> dict:
        try:
            data = json.loads(self._topics_file().read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("topics"), dict):
                data.setdefault("assigned", {})
                return data
        except (OSError, ValueError):
            pass
        return {"topics": {}, "assigned": {}}

    def _vector(self, text: str, idf_cache: dict) -> dict[str, float]:
        body = re.sub(r"\A---.*?\n---\n", "", text, flags=re.S)
        body = re.sub(r"```.*?```", " ", body, flags=re.S)[:12000]
        tf = Counter(w for w in _words(body) if not w.isdigit() and len(w) < 25 and w not in CHAT_NOISE)
        top = [w for w, _ in tf.most_common(60)]
        missing = [w for w in top if w not in idf_cache]
        if missing:
            idf_cache.update(self.brain._idf(missing))
        vec = {w: (1 + math.log(tf[w])) * idf_cache.get(w, 1.0) for w in top}
        best = dict(sorted(vec.items(), key=lambda x: -x[1])[:30])
        norm = math.sqrt(sum(v * v for v in best.values())) or 1.0
        return {w: v / norm for w, v in best.items()}

    @staticmethod
    def _cos(a: dict, b: dict) -> float:
        return sum(v * b.get(w, 0.0) for w, v in a.items())

    def group_conversations(self, agent: str = "cortex", only: list[str] | None = None, limit: int = 400,
                            threshold: float = 0.22) -> dict:
        """Put each new conversation into the topic it is most similar to (or start a topic), then
        rewrite the Topics/ notes that changed. Incremental: already-grouped chats are skipped."""
        b = self.brain
        b.sync()
        data = self.load_topics()
        topics, assigned = data["topics"], data["assigned"]
        paths = only or [r["path"] for r in b.db.execute(
            "SELECT path FROM notes WHERE path LIKE 'Conversations/%' ORDER BY path")]
        todo = [p for p in paths if p not in assigned][:limit]
        idf: dict = {}
        touched = set()
        for path in todo:
            text = b._text(path)
            if not text:
                continue
            vec = self._vector(text, idf)
            if not vec:
                continue
            best, score = None, 0.0
            for tid, t in topics.items():
                s = self._cos(vec, t["terms"])
                if s > score:
                    best, score = tid, s
            if best is None or score < threshold:
                words = [w for w, _ in sorted(vec.items(), key=lambda x: -x[1])[:3]]
                name = " ".join(w.capitalize() for w in words) or "Misc"
                best = "t-" + os.urandom(4).hex()
                existing = {t["name"].lower() for t in topics.values()}
                while name.lower() in existing:
                    name += " 2"
                topics[best] = {"name": name, "terms": vec, "members": [], "created": date.today().isoformat()}
            t = topics[best]
            t["members"].append(path)
            n = len(t["members"])
            merged = {w: t["terms"].get(w, 0) * (n - 1) / n + vec.get(w, 0) / n for w in set(t["terms"]) | set(vec)}
            keep = dict(sorted(merged.items(), key=lambda x: -x[1])[:40])
            norm = math.sqrt(sum(v * v for v in keep.values())) or 1.0
            t["terms"] = {w: round(v / norm, 4) for w, v in keep.items()}
            assigned[path] = best
            touched.add(best)
        for tid, t in topics.items():  # forget deleted conversations
            alive = [m for m in t["members"] if (b.root / m).exists()]
            if len(alive) != len(t["members"]):
                t["members"] = alive
                touched.add(tid)
        written = []
        for tid in sorted(touched):
            t = topics[tid]
            if t["members"]:
                written.append(self._write_topic(t, agent))
        tmp = self._topics_file().with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self._topics_file())
        return {"grouped": len(todo), "topics": len(topics), "touched": written,
                "remaining": max(0, len([p for p in paths if p not in assigned]))}

    def _write_topic(self, t: dict, agent: str) -> str:
        b = self.brain
        from .importers import _safe
        rel = f"Topics/{_safe(t['name'], 60)}.md"
        terms = t["terms"]
        sentences, decisions, actions, rows = [], [], [], []
        for m in t["members"][-60:]:
            text = b._text(m)
            meta_title = re.search(r"^# (.+)$", text, re.M)
            day = re.search(r"(\d{4}-\d{2}-\d{2})", m)
            src = m.split("/")[1] if m.count("/") >= 2 else ""
            rows.append(f"- {day.group(1) if day else ''} [[{m[:-3]}|{(meta_title.group(1) if meta_title else Path(m).stem)[:80]}]]"
                        + (f" · {src}" if src else ""))
            body = re.sub(r"\A---.*?\n---\n", "", text, flags=re.S)
            body = re.sub(r"```.*?```", " ", body, flags=re.S)
            for line in body.splitlines():
                line = line.strip()
                if not line or line.startswith(("**", "#", "|", ">")) or len(line) < 30:
                    continue
                for s in re.split(r"(?<=[.!?])\s+", line)[:6]:
                    if 30 <= len(s) <= 260:
                        ws = _words(s)
                        if ws:
                            sentences.append((sum(terms.get(w, 0) for w in set(ws)) / (len(set(ws)) ** 0.5), s))
                        if TYPE_PATTERNS["decision"].search(s):
                            decisions.append(s)
            actions += [x["text"] for x in find_tasks(body)[:5]]
        seen, summary = set(), []
        for _, s in sorted(sentences, key=lambda x: -x[0]):
            k = _norm(s)[:80]
            if k not in seen:
                seen.add(k)
                summary.append(s)
            if len(summary) >= 5:
                break
        block = ["<!-- cortex:topic (rewritten by Cortex as conversations arrive; write your own notes outside this block) -->",
                 f"Key terms: {', '.join(list(terms)[:8])}", "", "## Summary"]
        block += [f"- {s}" for s in summary] or ["- (not enough text yet)"]
        if decisions:
            block += ["", "## Decisions mentioned"] + [f"- {d}" for d in list(dict.fromkeys(decisions))[:8]]
        if actions:
            block += ["", "## Action items mentioned"] + [f"- {a}" for a in list(dict.fromkeys(actions))[:8]]
        block += ["", f"## Conversations ({len(t['members'])})"] + rows[::-1][:60] + ["<!-- /cortex:topic -->"]
        gen = "\n".join(block)
        p = b.resolve(rel)
        if p.exists():
            old = p.read_text(encoding="utf-8", errors="replace")
            if "<!-- cortex:topic" in old and "<!-- /cortex:topic -->" in old:
                a = old.index("<!-- cortex:topic")
                z = old.index("<!-- /cortex:topic -->") + len("<!-- /cortex:topic -->")
                new = old[:a] + gen + old[z:]
            else:
                new = old.rstrip("\n") + "\n\n" + gen + "\n"
            if new != old:
                b.write(rel, new, agent=agent, mode="overwrite", log=False)
        else:
            b.write(rel, f"---\ntype: topic\ntags: [topic]\n---\n# {t['name']}\n\n{gen}\n", agent=agent, mode="create", log=False)
        return rel
