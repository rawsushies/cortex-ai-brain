"""Graph analysis (centrality, clusters) and Cortex speaking first (push events, change feed)."""

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["CORTEX_SEMANTIC"] = "off"
os.environ.setdefault("CORTEX_LOCAL_DIR", tempfile.mkdtemp(prefix="cortex-test-local-"))

from cortex import graph  # noqa: E402
from cortex.core import Brain  # noqa: E402
from cortex.mcp_server import Server  # noqa: E402


class GraphTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.b = Brain(Path(self.tmp.name) / "v")
        w = lambda p, t: self.b.write(p, t, agent="you", log=False)
        # Two triangles joined by one bridge note, plus an orphan and a missing link.
        w("A1.md", "# A1\n[[A2]] [[A3]]")
        w("A2.md", "# A2\n[[A3]]")
        w("A3.md", "# A3\n[[Bridge]]")
        w("Bridge.md", "# Bridge\n[[B1]]")
        w("B1.md", "# B1\n[[B2]] [[B3]]")
        w("B2.md", "# B2\n[[B3]] [[Ghost]]")
        w("B3.md", "# B3\nbudget invoice payment bank")
        w("Lonely.md", "# Lonely\nno links")

    def tearDown(self):
        self.b.close()
        self.tmp.cleanup()

    def test_bridge_has_highest_betweenness_and_clusters_split(self):
        data = graph.analyze(self.b)
        n = data["nodes"]
        top = max((k for k in n if not n[k].get("missing")), key=lambda k: n[k]["betweenness"])
        self.assertIn(top, ("Bridge.md", "A3.md", "B1.md"))  # the path through the middle
        self.assertEqual(n["Bridge.md"]["betweenness"], 1.0)
        self.assertEqual(n["A1.md"]["cluster"], n["A2.md"]["cluster"])
        self.assertNotEqual(n["A1.md"]["cluster"], n["B2.md"]["cluster"])
        self.assertEqual(data["orphans"], ["Lonely.md"])
        self.assertEqual(data["missing"], ["Ghost"])
        self.assertTrue(data["exact"])

    def test_cache_and_invalidation(self):
        a = graph.analyze(self.b)
        self.assertIs(graph.analyze(self.b), a)
        self.b.write("A2.md", "# A2\n[[A3]] [[Lonely]]", agent="you")
        self.assertNotIn("Lonely.md", graph.analyze(self.b)["orphans"])

    def test_tool_and_focus(self):
        s = Server(self.b, "core")
        r = s.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                      "params": {"name": "brain_graph", "arguments": {"metric": "betweenness", "limit": 3}}})["result"]
        self.assertEqual(r["structuredContent"]["nodes"][0]["id"], "Bridge.md")
        r = s.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                      "params": {"name": "brain_graph", "arguments": {"focus": "B1", "depth": 1}}})["result"]
        ids = {x["id"] for x in r["structuredContent"]["nodes"]}
        self.assertEqual(ids, {"B1.md", "B2.md", "B3.md", "Bridge.md"})
        self.assertTrue(all(e["source"] in ids and e["target"] in ids for e in r["structuredContent"]["edges"]))

    def test_sampled_mode_on_big_graphs(self):
        old = graph.EXACT_LIMIT
        graph.EXACT_LIMIT = 3
        try:
            self.b._graph_cache = None
            data = graph.analyze(self.b)
            self.assertFalse(data["exact"])
            self.assertTrue(all(0 <= v["betweenness"] <= 1 for v in data["nodes"].values()))
        finally:
            graph.EXACT_LIMIT = old
            self.b._graph_cache = None

    def test_superseded_decisions_in_health(self):
        self.b.write("Projects/Site.md", "# Site\n## Decisions\n"
                     "- 2026-01-02 We decided to host the site on shared hosting [type:: decision]\n"
                     "- 2026-03-04 We decided to host the site on a VPS instead [type:: decision]\n", agent="you")
        h = self.b.health()
        self.assertEqual(len(h["decisions_to_review"]), 1)
        self.assertIn("VPS", h["decisions_to_review"][0]["later"])


class ChangesFeedTest(unittest.TestCase):
    def test_cursor(self):
        with tempfile.TemporaryDirectory() as t:
            b = Brain(Path(t) / "v")
            s = Server(b, "core")
            call = lambda **a: s.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                         "params": {"name": "brain_changes", "arguments": a}})["result"]["structuredContent"]
            b.write("A.md", "# A", agent="claude")
            first = call()
            self.assertEqual(first["changesets"][0]["actor"], "claude")
            time.sleep(1.1)
            (b.root / "Outside.md").write_text("# Outside\nedited in Obsidian")
            b._last_sync = 0
            nxt = call(since=first["cursor"])
            self.assertEqual(nxt["changesets"], [])
            self.assertEqual([o["path"] for o in nxt["outside"]], ["Outside.md"])
            b.close()


class EventsTest(unittest.TestCase):
    """Over a real stdio connection, as COre would subscribe."""

    def test_push_events(self):
        with tempfile.TemporaryDirectory() as t:
            v = Path(t) / "v"
            b = Brain(v)
            b.add_task("File the tax form", due=(date.today() - timedelta(days=1)).isoformat(), agent="you")
            b.close()
            env = {**os.environ, "CORTEX_AGENT": "core", "CORTEX_AUTO_IMPORT": "off", "CORTEX_EVENT_INTERVAL": "0.3"}
            p = subprocess.Popen([sys.executable, str(ROOT / "run.py"), "mcp", "--vault", str(v)], env=env,
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                                 encoding="utf-8")
            try:
                send = lambda o: (p.stdin.write(json.dumps(o) + "\n"), p.stdin.flush())
                send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                    "protocolVersion": "2025-06-18", "clientInfo": {"name": "core"},
                    "capabilities": {"experimental": {"cortex": {"events": True}}}}})
                self.assertIn("result", json.loads(p.stdout.readline()))
                send({"jsonrpc": "2.0", "method": "notifications/initialized"})
                due = json.loads(p.stdout.readline())
                self.assertEqual(due["method"], "notifications/cortex/task_due")
                self.assertEqual(due["params"]["tasks"][0]["text"].split(" 📅")[0], "File the tax form")
                # COre's own write: no event back (it knows; and no loops)
                send({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                      "params": {"name": "brain_capture", "arguments": {"text": "hello", "actor": "agent"}}})
                self.assertEqual(json.loads(p.stdout.readline())["id"], 2)
                # an edit made in Obsidian: pushed
                time.sleep(0.4)
                (v / "Obsidian edit.md").write_text("# Obsidian edit\nwritten outside Cortex")
                ev = json.loads(p.stdout.readline())
                self.assertEqual(ev["method"], "notifications/cortex/changed")
                self.assertEqual(ev["params"]["paths"], ["Obsidian edit.md"])
                self.assertTrue(ev["params"]["outside"])
            finally:
                p.stdin.close()
                p.wait(10)
                p.stdout.close()



class NotifyTest(unittest.TestCase):
    def test_loopback_only(self):
        from unittest import mock
        from cortex import notify
        for url, ok in (("http://127.0.0.1:47321/x", True), ("http://localhost:1/x", True),
                        ("http://example.com/x", False), ("https://127.0.0.1/x", False), ("", False)):
            with mock.patch.dict(os.environ, {"CORTEX_NOTIFY_URL": url}):
                self.assertEqual(bool(notify.notify_url()), ok, url)


if __name__ == "__main__":
    unittest.main()
