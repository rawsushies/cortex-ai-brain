"""Self-organizing memory: dates, tasks, categories, links, dedup, ingest, topics. No AI involved."""

import json
import os
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["CORTEX_SEMANTIC"] = "off"
os.environ.setdefault("CORTEX_LOCAL_DIR", tempfile.mkdtemp(prefix="cortex-test-local-"))

from cortex.core import Brain  # noqa: E402
from cortex.dates import find_date  # noqa: E402
from cortex.mcp_server import Server  # noqa: E402
from cortex.organize import (Organizer, autolink, detect_type, find_entities, find_tasks,  # noqa: E402
                             lexicon_category)

THU = date(2026, 11, 12)  # a Thursday


class DatesTest(unittest.TestCase):
    def test_english_and_malay(self):
        cases = {
            "pay rent by Friday": "2026-11-13", "hantar laporan esok": "2026-11-13", "call mum lusa": "2026-11-14",
            "renew passport 12 Dec": "2026-12-12", "jumaat depan jumpa client": "2026-11-20",
            "bayar bil hujung bulan": "2026-11-30", "submit in 3 days": "2026-11-15", "dalam dua minggu": "2026-11-26",
            "flight on 5/1": "2027-01-05", "March 3rd launch": "2027-03-03", "due 2026-12-01": "2026-12-01",
            "next week review": "2026-11-16", "Friday": "2026-11-13",
        }
        for text, want in cases.items():
            self.assertEqual(str(find_date(text, THU)[0]), want, text)

    def test_no_false_dates(self):
        for text in ("we sat in the sun", "I may go", "the march of progress", "version 3.2 shipped"):
            self.assertIsNone(find_date(text, THU)[0], text)


class TasksTest(unittest.TestCase):
    def test_triggers(self):
        got = find_tasks("Talked to Sam. I need to send the invoice by Friday. Remind me to call the bank tomorrow.\n"
                         "Jangan lupa bayar bil elektrik esok. Kena siapkan slide.\nDo I need to renew it?\n"
                         "- [ ] already a task\n```\nneed to ignore code\n```", THU)
        self.assertEqual([(t["text"], t["due"]) for t in got], [
            ("Send the invoice", "2026-11-13"), ("Call the bank", "2026-11-13"),
            ("Bayar bil elektrik", "2026-11-13"), ("Siapkan slide", None)])

    def test_imperative_needs_a_date(self):
        self.assertEqual(find_tasks("Pay rent by Friday", THU)[0]["text"], "Pay rent")
        self.assertEqual(find_tasks("Pay attention to the details", THU), [])

    def test_types(self):
        self.assertEqual(detect_type("We decided to use SQLite for the index"), "decision")
        self.assertEqual(detect_type("Kita pilih Penang untuk retreat"), "decision")
        self.assertEqual(detect_type("Lesson learned: back up before migrating"), "lesson")
        self.assertEqual(detect_type("What if the orb changed colour with mood?"), "idea")
        self.assertEqual(detect_type("Remind me to water the plants"), "task")
        self.assertEqual(detect_type("The office wifi password changed"), "note")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.b = Brain(Path(self.tmp.name) / "v")
        self.org = Organizer(self.b)

    def tearDown(self):
        self.b.close()
        self.tmp.cleanup()


class CategoryTest(Base):
    def test_lexicon(self):
        self.assertEqual(lexicon_category("Q3 budget", "invoice payment bank")[0], "Finance")
        self.assertEqual(lexicon_category("", "flight to Penang, book the hotel and check the visa")[0], "Travel")
        self.assertIsNone(lexicon_category("", "hello there")[0])

    def test_rules_win_and_index_has_categories(self):
        (self.b.meta_dir / "rules.json").write_text(json.dumps(
            {"rules": [{"match": {"keywords": ["acme"]}, "category": "Clients", "folder": "Clients"}]}))
        self.b.write("Notes/Acme call.md", "# Acme call\nbudget invoice payment", agent="you")
        self.b.write("Trip.md", "# Trip\nflight hotel itinerary passport", agent="you")
        cats = {c["category"]: c["count"] for c in self.b.categories()}
        self.assertEqual(cats, {"Clients": 1, "Travel": 1})
        r = self.org.ingest("Acme wants the proposal revised; they liked the timeline overall and the pricing",
                            agent="core", today=THU)
        self.assertEqual((r["category"], r["why"]), ("Clients", "rule"))


class LinksTest(Base):
    def test_entities_and_autolink(self):
        self.b.write("Projects/Website Relaunch.md", "# Website Relaunch\nx", agent="you")
        self.b.write("People/Sam Lee.md", "# Sam Lee\nx", agent="you")
        self.b.write("Ideas.md", "# Ideas\nx", agent="you")
        text = "Sam Lee says the website relaunch slips a week. ideas welcome. `Sam Lee` in code"
        ents = find_entities(self.b, text)
        self.assertEqual(sorted(e["path"] for e in ents), ["People/Sam Lee.md", "Projects/Website Relaunch.md"])
        out = autolink(self.b, text, ents)
        self.assertIn("[[Sam Lee]] says the [[website relaunch]] slips", out)  # links are case-insensitive
        self.assertIn("`Sam Lee` in code", out)


class IngestTest(Base):
    def setUp(self):
        super().setUp()
        self.s = Server(self.b, "core")

    def call(self, name, **args):
        r = self.s.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": args}})
        return r["result"]

    def test_decision_goes_to_project_and_is_a_record(self):
        self.b.write("Projects/Website.md", "# Website\nThe site.", agent="you")
        r = self.org.ingest("We decided to host the Website on a small VPS", agent="core/forge", today=THU)
        self.assertEqual((r["type"], r["path"], r["project"]), ("decision", "Projects/Website.md", "Projects/Website.md"))
        text = (self.b.root / "Projects/Website.md").read_text()
        self.assertIn("## Decisions\n- 2026-11-12 We decided to host the Website on a small VPS [type:: decision]", text)
        self.assertIn("[by:: core/forge]", text)
        recs = self.call("brain_records", type="decision")["structuredContent"]["records"]
        self.assertEqual(recs[0]["path"], "Projects/Website.md")
        self.assertEqual(recs[0]["by"], "core/forge")

    def test_task_and_duplicate(self):
        r = self.call("brain_ingest", text="Remind me to renew the car insurance by Friday")["structuredContent"]
        self.assertEqual(r["type"], "task")
        self.assertEqual(r["tasks"][0]["text"], "Renew the car insurance")
        self.assertTrue(r["tasks"][0]["due"])
        self.assertEqual(len(self.b.tasks("open")), 1)
        again = self.call("brain_ingest", text="remind me to renew the car insurance by friday")["structuredContent"]
        self.assertEqual(again["action"], "duplicate")
        self.assertEqual(len(self.b.tasks("open")), 1)

    def test_long_note_to_inbox_with_category_and_undo(self):
        long = ("Notes from the gym consultation.\n\n" + "The trainer suggested a workout plan with three sessions a week, "
                "more protein and better sleep. " * 8 + "\n\nI need to book a doctor appointment next week.")
        r = self.call("brain_ingest", text=long, source="desktop", actor="agent", verified=True)["structuredContent"]
        self.assertTrue(r["path"].startswith("Inbox/Notes from the gym consultation"))
        self.assertEqual(r["category"], "Health")
        note = (self.b.root / r["path"]).read_text()
        self.assertIn("category: Health", note)
        self.assertIn("source: desktop", note)
        self.assertIn("- [ ] Book a doctor appointment 📅 2026-", note)
        self.call("brain_undo", changeset=r["changeset"])
        self.assertFalse((self.b.root / r["path"]).exists())

    def test_web_source_is_marked_external(self):
        self.org.ingest("Hosting prices rose 20% this year", agent="core", source="web", today=THU)
        rec = self.b.recall("hosting prices", 400)
        self.assertTrue(rec["passages"][0]["external"])

    def test_conversations_group_into_topics(self):
        chat = ("**You**\n\nHow should I structure the sqlite index for the search engine?\n\n**Claude**\n\n"
                "Use an FTS5 virtual table for the search index; keep the sqlite database outside the synced folder. "
                "We decided to use contentless FTS5 for the index.\n")
        r1 = self.org.ingest(chat, agent="core", kind="conversation", title="Sqlite index design", today=THU)
        r2 = self.org.ingest(chat.replace("structure", "speed up") + " The index rebuild with sqlite FTS5 is fast.",
                             agent="core", kind="conversation", title="Faster sqlite index", today=THU)
        r3 = self.org.ingest("**You**\n\nBest hiking trails near the mountains with waterfalls?\n\n**Claude**\n\n"
                             "Try the waterfall trail; hiking boots recommended for the mountain paths.",
                             agent="core", kind="conversation", title="Hiking", today=THU)
        self.assertTrue(r1["path"].startswith("Conversations/"))
        self.assertEqual(r1["topics"], r2["topics"])
        self.assertNotEqual(r1["topics"], r3["topics"])
        topic = (self.b.root / r1["topics"][0]).read_text()
        self.assertIn("## Conversations (2)", topic)
        self.assertIn("## Decisions mentioned", topic)
        topics = self.call("brain_topics")["structuredContent"]["topics"]
        self.assertEqual(sorted(t["conversations"] for t in topics), [1, 2])
        self.assertEqual(self.b.search("waterfall trail", scope="notes"), [])  # history stays out of notes scope

    def test_topic_note_keeps_user_text(self):
        chat = "**You**\n\nGarden compost tips for tomatoes please\n\n**Claude**\n\nCompost tomatoes with garden mulch."
        r = self.org.ingest(chat, agent="core", kind="conversation", today=THU)
        p = self.b.root / r["topics"][0]
        p.write_text(p.read_text() + "\nMy own note: buy mulch.\n")
        self.org.ingest(chat + " More compost and garden tomatoes.", agent="core", kind="conversation", today=THU)
        text = p.read_text()
        self.assertIn("My own note: buy mulch.", text)
        self.assertIn("## Conversations (2)", text)

    def test_entity_stub_after_three_mentions(self):
        for text in ("Lunch with Nora Hassan about the budget plan", "Call notes: Nora Hassan prefers email updates",
                     "Met Nora Hassan at the conference, she runs logistics"):
            r = self.org.ingest(text, agent="core", today=THU)
        self.assertEqual(r["stubs"], ["Entities/Nora Hassan.md"])
        r = self.org.ingest("Nora Hassan sent the contract over today for review", agent="core", today=THU)
        self.assertIn("Entities/Nora Hassan.md", r["links"])


if __name__ == "__main__":
    unittest.main()
