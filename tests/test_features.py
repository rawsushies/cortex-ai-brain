"""Tests for history, editing, moving, tasks, health, recap, conflicts, prompts and semantic search."""

import io
import json
import os
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
import unittest.mock
import zlib
from datetime import date, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["CORTEX_SEMANTIC"] = "off"
os.environ.setdefault("CORTEX_LOCAL_DIR", tempfile.mkdtemp(prefix="cortex-test-local-"))

from cortex.core import SCHEMA_VERSION, Brain, ConflictError  # noqa: E402
from cortex.mcp_server import Server  # noqa: E402


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.v = Path(self.tmp.name)
        self.b = Brain(self.v)

    def tearDown(self):
        self.b.close()
        self.tmp.cleanup()

    def note(self, rel, text):
        p = self.v / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        self.b.sync(force=True)


class HistoryTest(Base):
    def test_every_change_is_versioned_and_restorable(self):
        self.b.write("A.md", "# A\nv1", agent="claude")
        self.b.write("A.md", "# A\nv2", agent="codex")
        self.b.edit("A.md", "v2", "v3", agent="gemini")
        hist = self.b.history("A.md")
        self.assertEqual(len(hist), 2)  # before v2, before v3
        self.assertEqual(hist[0]["replaced_by"], "gemini")
        self.assertIn("v2", self.b.version("A.md", hist[0]["id"]))
        self.b.restore("A.md", hist[1]["id"])
        self.assertIn("v1", (self.v / "A.md").read_text())
        self.assertEqual(self.b.activity(1)[0]["action"], "restored")

    def test_version_id_cannot_escape(self):
        self.b.write("A.md", "x")
        with self.assertRaises(ValueError):
            self.b.version("A.md", "../../index.db")

    def test_history_is_capped(self):
        for i in range(35):
            self.b.write("A.md", f"v{i}")
        self.assertEqual(len(self.b.history("A.md")), 30)


class EditTest(Base):
    def test_find_replace(self):
        self.b.write("A.md", "# A\nalpha beta alpha")
        with self.assertRaises(ValueError):  # ambiguous
            self.b.edit("A", "alpha", "x")
        with self.assertRaises(ValueError):  # missing
            self.b.edit("A", "zzz", "x")
        self.b.edit("A", "beta", "gamma")
        self.assertEqual(self.b.edit("A", "alpha", "z", all=True)["replacements"], 2)
        self.assertIn("z gamma z", (self.v / "A.md").read_text())

    def test_sections(self):
        self.b.write("P.md", "# P\n\n## Status\nold\n\n## Decisions\n- d1\n\n## Notes\nkeep")
        self.b.write_section("P", "Status", "new status")
        self.b.write_section("P", "Decisions", "- d2", mode="append")
        self.b.write_section("P", "Risks", "- r1")  # created
        t = (self.v / "P.md").read_text()
        self.assertIn("## Status\nnew status\n", t)
        self.assertNotIn("old", t)
        self.assertIn("- d1\n- d2", t)
        self.assertIn("## Notes\nkeep", t)
        self.assertTrue(t.rstrip().endswith("## Risks\n- r1"))

    def test_conflict_detection(self):
        self.b.write("A.md", "mine")
        m = (self.v / "A.md").stat().st_mtime
        time.sleep(0.01)
        self.b.write("A.md", "ai changed it", agent="claude")
        os.utime(self.v / "A.md", (m + 5, m + 5))
        with self.assertRaises(ConflictError):
            self.b.write("A.md", "my edit", expect_mtime=m)
        self.assertEqual(self.b.write("A.md", "my edit")["action"], "updated")  # forced

    def test_duplicate_warning(self):
        self.b.write("People/Maria Lopez.md", "# Maria Lopez")
        r = self.b.write("Inbox/maria-lopez.md", "dup")
        self.assertEqual(r["similar"], ["People/Maria Lopez.md"])
        self.assertNotIn("similar", self.b.write("Inbox/Totally new.md", "x"))


class MoveTest(Base):
    def test_move_rewrites_links(self):
        self.note("Projects/Site.md", "# Site")
        self.note("A.md", "See [[Site]] and [[Site#Plan|the plan]] and ![[Site]].")
        self.note("B.md", "Path link [[Projects/Site]]. Unrelated [[Other]].")
        r = self.b.move("Site", "Archive/Website 2025")
        self.assertEqual(r["path"], "Archive/Website 2025.md")
        self.assertEqual(sorted(r["links_updated"]), ["A.md", "B.md"])
        self.assertIn("[[Website 2025]] and [[Website 2025#Plan|the plan]] and ![[Website 2025]]", (self.v / "A.md").read_text())
        self.assertIn("[[Website 2025]]. Unrelated [[Other]]", (self.v / "B.md").read_text())
        self.assertEqual(sorted(self.b.backlinks("Archive/Website 2025.md")), ["A.md", "B.md"])
        self.assertFalse((self.v / "Projects/Site.md").exists())

    def test_move_refuses_overwrite(self):
        self.note("A.md", "a")
        self.note("B.md", "b")
        with self.assertRaises(FileExistsError):
            self.b.move("A", "B")


class TaskTest(Base):
    def test_tasks_lifecycle(self):
        past = (date.today() - timedelta(days=3)).isoformat()
        self.note("P.md", f"# P\n- [ ] write report 📅 {past}\n- [x] done thing\n```\n- [ ] not a task in code\n```\n- [ ] call Ana due:2099-01-01")
        open_ = self.b.tasks("open")
        self.assertEqual([t["text"][:12] for t in open_], ["write report", "call Ana due"])
        self.assertTrue(open_[0]["overdue"])
        self.assertEqual(open_[1]["due"], "2099-01-01")
        self.b.toggle_task("P", open_[0]["line"], open_[0]["text"], agent="claude")
        self.assertIn("- [x] write report", (self.v / "P.md").read_text())
        self.assertEqual(self.b.activity(1)[0]["action"], "completed task")
        # stale line number: found by text
        (self.v / "P.md").write_text("# P\n\nnew line\n" + (self.v / "P.md").read_text().split("\n", 1)[1])
        self.b.sync(force=True)
        self.b.toggle_task("P", 1, "call Ana due:2099-01-01")
        self.assertIn("- [x] call Ana", (self.v / "P.md").read_text())

    def test_add_task(self):
        r = self.b.add_task("buy milk", due="2030-05-01")
        self.assertTrue(r["path"].startswith("Daily/"))
        self.assertEqual(self.b.tasks("open")[0]["due"], "2030-05-01")
        self.b.add_task("ship v2", path="Projects/App")
        self.assertIn("## Tasks\n- [ ] ship v2", (self.v / "Projects/App.md").read_text())
        with self.assertRaises(ValueError):
            self.b.add_task("x", due="tomorrow")


class HealthRecapTest(Base):
    def test_health(self):
        self.note("A.md", "# A\nlinks to [[B]] and [[Missing]] plus enough words here")
        self.note("B.md", "# B\nback to [[A]] with enough words here too")
        self.note("Lonely.md", "# Lonely\nno links at all in this one ok")
        self.note("Empty.md", "")
        self.note("x/A.md", "# A\nsame name in another folder")
        old = time.time() - 100 * 86400
        self.note("Proj.md", "---\nstatus: active\n---\n# Proj\nan old active project with words")
        os.utime(self.v / "Proj.md", (old, old))
        h = self.b.health()
        self.assertEqual(h["broken_links"], [{"note": "A.md", "link": "Missing"}])
        self.assertIn(["A.md", "x/A.md"], h["duplicates"])
        self.assertEqual([s["note"] for s in h["stale"]], ["Proj.md"])
        self.assertIn("Empty.md", h["empty"])
        self.assertIn("Lonely.md", h["orphans"])
        self.assertNotIn("A.md", h["orphans"])
        self.assertLess(h["score"], 100)

    def test_links_in_code_are_ignored(self):
        self.note("Guide.md", "# Guide\nWrite `[[Wikilinks]]` like this.\n```\n[[Also not a link]]\n```\nReal: [[Target]]")
        self.note("Target.md", "# Target\nhere")
        self.assertEqual(self.b.health()["broken_links"], [])
        self.assertEqual(self.b.backlinks("Target.md"), ["Guide.md"])

    def test_recap(self):
        self.b.write("New.md", "# New", agent="claude")
        self.b.log("codex", "decided", "New.md", "use SQLite")
        r = self.b.recap(7)
        self.assertEqual([n["path"] for n in r["created"]], ["New.md"])
        self.assertEqual(r["by_agent"], {"claude": 1, "codex": 1})
        self.assertEqual(r["decisions_and_work"][0]["note"], "use SQLite")


class SchemaTest(unittest.TestCase):
    def test_old_index_is_rebuilt(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / ".cortex").mkdir()
            db = sqlite3.connect(Path(d) / ".cortex" / "index.db")
            db.execute("CREATE TABLE notes(path TEXT PRIMARY KEY, title TEXT)")  # v1 layout
            db.commit()
            db.close()
            (Path(d) / "N.md").write_text("# N\nhello")
            b = Brain(d)
            self.assertEqual(b.db.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)
            self.assertEqual(b.search("hello")[0].path, "N.md")
            b.close()


class McpFeaturesTest(Base):
    def call(self, s, name, **args):
        r = s.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": args}})
        return r["result"]["content"][0]["text"], r["result"].get("isError", False)

    def test_new_tools(self):
        s = Server(self.b, "claude")
        self.call(s, "brain_write", path="Projects/App.md", content="# App\n\n## Status\nplanning", mode="create")
        self.assertIn("edited section", self.call(s, "brain_edit", path="App", section="Status", content="building")[0])
        self.assertIn("1 replacement", self.call(s, "brain_edit", path="App", find="building", replace="shipped")[0])
        txt, err = self.call(s, "brain_edit", path="App", find="nope", replace="x")
        self.assertTrue(err)
        self.assertIn("added task", self.call(s, "brain_tasks", action="add", text="write docs", path="App")[0])
        self.assertIn("write docs", self.call(s, "brain_tasks")[0])
        self.assertIn("completed", self.call(s, "brain_tasks", action="done", text="docs")[0])
        self.assertIn("moved", self.call(s, "brain_move", path="App", to="Archive/App")[0])
        self.assertIn("Health score", self.call(s, "brain_health")[0])
        self.assertIn("Last 7 days", self.call(s, "brain_recap")[0])
        self.assertIn("similar notes already exist", self.call(s, "brain_write", path="Inbox/app.md", content="x")[0])
        txt, err = self.call(s, "brain_edit", path="Archive/App")
        self.assertTrue(err)
        self.assertIn("decided", self.call(s, "brain_log", summary="Go with plan B", kind="decision") and self.b.activity(1)[0]["action"])

    def test_prompts(self):
        s = Server(self.b, "claude")
        lst = s.handle({"jsonrpc": "2.0", "id": 1, "method": "prompts/list"})["result"]["prompts"]
        self.assertEqual({p["name"] for p in lst}, {"save", "decide", "person", "project", "review", "tidy", "challenge", "research", "pulse"})
        got = s.handle({"jsonrpc": "2.0", "id": 2, "method": "prompts/get",
                        "params": {"name": "decide", "arguments": {"decision": "Use {braces} safely"}}})["result"]
        text = got["messages"][0]["content"]["text"]
        self.assertIn("Use {braces} safely", text)
        self.assertIn("infer from our conversation", text)
        self.assertIn("days=7", s.handle({"jsonrpc": "2.0", "id": 3, "method": "prompts/get", "params": {"name": "review"}})["result"]["messages"][0]["content"]["text"])
        init = s.handle({"jsonrpc": "2.0", "id": 4, "method": "initialize", "params": {}})["result"]
        self.assertIn("prompts", init["capabilities"])


# ── semantic search against a fake Ollama ───────────────────────────────
SYN = {"automobile": "car", "vehicle": "car", "fixing": "repair", "repairs": "repair", "mend": "repair"}


def fake_vec(text, dim=512):
    v = [0.0] * dim
    for w in text.lower().replace(":", " ").split():
        w = SYN.get(w.strip(".,"), w.strip(".,"))
        if w in ("search_query", "search_document"):
            continue
        v[zlib.crc32(w.encode()) % dim] += 1.0  # deterministic, unlike hash()
    return v


class FakeOllama(BaseHTTPRequestHandler):
    calls = 0

    def log_message(self, *a):
        pass

    def do_GET(self):
        body = json.dumps({"models": [{"name": "nomic-embed-text:latest"}]}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        FakeOllama.calls += 1
        req = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        body = json.dumps({"embeddings": [fake_vec(t) for t in req["input"]]}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class SemanticTest(unittest.TestCase):
    def setUp(self):
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeOllama)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        os.environ["OLLAMA_HOST"] = f"http://127.0.0.1:{self.srv.server_port}"
        os.environ["CORTEX_SEMANTIC"] = "on"
        self.tmp = tempfile.TemporaryDirectory()
        v = Path(self.tmp.name)
        (v / "Garage.md").write_text("# Garage\nFixing the automobile brakes next week.")
        (v / "Cooking.md").write_text("# Cooking\nPasta with tomato sauce.")
        (v / "Trip.md").write_text("# Trip\nDrive the vehicle to the coast.")
        self.b = Brain(v)
        self.b.sync(force=True)
        self.wait_embedded()

    def wait_embedded(self):
        for _ in range(100):
            st = self.b.semantic.status()
            if st["indexed"] == st["total"]:
                return
            time.sleep(0.05)
        self.fail(f"embedding did not finish: {self.b.semantic.status()}")

    def tearDown(self):
        self.b.close()
        self.srv.shutdown()
        self.srv.server_close()
        os.environ["CORTEX_SEMANTIC"] = "off"
        os.environ.pop("OLLAMA_HOST", None)
        self.tmp.cleanup()

    def test_meaning_search_finds_synonyms(self):
        st = self.b.semantic.status()
        self.assertTrue(st["on"])
        self.assertEqual(st["indexed"], 3)
        hits = self.b.search("car repair")  # no keyword overlap with "Fixing the automobile"
        self.assertEqual(hits[0].path, "Garage.md")
        self.assertEqual(hits[0].via, ["meaning"])
        self.assertNotIn("Cooking.md", [h.path for h in hits[:2]])

    def test_hybrid_and_reembed_on_change(self):
        hits = self.b.search("brakes")  # keyword + meaning agree
        self.assertEqual(hits[0].path, "Garage.md")
        self.assertEqual(sorted(hits[0].via), ["keyword", "meaning"])
        before = FakeOllama.calls
        self.b.write("Cooking.md", "# Cooking\nNow about mending the automobile.", agent="you")
        for _ in range(100):  # the background worker re-embeds the changed note
            if "Cooking.md" in [h.path for h in self.b.search("car repair")[:2]]:
                break
            time.sleep(0.05)
        self.assertGreater(FakeOllama.calls, before)
        self.assertIn("Cooking.md", [h.path for h in self.b.search("car repair")[:2]])

    def test_related(self):
        rel = self.b.related("Garage.md")
        self.assertEqual(rel[0].path, "Trip.md")  # both about cars, not linked


class SemanticOffTest(Base):
    def test_absent_ollama_is_silent(self):
        os.environ["CORTEX_SEMANTIC"] = "on"
        os.environ["OLLAMA_HOST"] = "http://127.0.0.1:9"  # nothing listens here
        try:
            b = Brain(self.v)
            (self.v / "N.md").write_text("# N\nhello world")
            b.sync(force=True)
            self.assertFalse(b.semantic.status()["on"])
            self.assertEqual(b.search("hello")[0].path, "N.md")
            self.assertEqual(b.related("N.md"), [])
            b.close()
        finally:
            os.environ["CORTEX_SEMANTIC"] = "off"
            os.environ.pop("OLLAMA_HOST", None)


if __name__ == "__main__":
    unittest.main()


class FallbackLayoutTest(unittest.TestCase):
    def test_older_sqlite_layout_still_works(self):
        """Pythons bundling SQLite < 3.43 can't build the slim contentless index; the fallback must behave the same."""
        import cortex.core as C
        with tempfile.TemporaryDirectory() as d, unittest.mock.patch.object(C, "FTS_LAYOUTS", C.FTS_LAYOUTS[1:]):
            (Path(d) / "A.md").write_text("# Alpha\nthe quick brown fox")
            b = Brain(d)
            b.sync(force=True)
            self.assertEqual(b.search("quick fox")[0].path, "A.md")
            self.assertIn("«quick»", b.search("quick")[0].snippet)
            b.write("A.md", "# Alpha\nnow about otters")
            self.assertEqual(b.search("fox"), [])
            self.assertEqual(b.search("otters")[0].path, "A.md")
            b.delete("A.md")
            self.assertEqual(b.search("otters"), [])
            b.close()
