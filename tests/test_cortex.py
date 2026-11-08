"""Run: python -m unittest discover tests"""

import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ['CORTEX_SEMANTIC'] = 'off'  # semantic is tested separately with a mock Ollama
os.environ.setdefault("CORTEX_LOCAL_DIR", tempfile.mkdtemp(prefix="cortex-test-local-"))
from cortex.core import Brain, ConflictError, extract_section  # noqa: E402
from cortex.mcp_server import Server  # noqa: E402
from cortex.web import make_handler  # noqa: E402


class CoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.v = Path(self.tmp.name)
        (self.v / "Projects").mkdir()
        (self.v / "Projects" / "Website.md").write_text(
            "---\ntags: [work, web]\n---\n# Website redesign\n\nUsing **Astro**. Talk to [[Maria]].\n\n## Decisions\nHosting on Netlify.\n\n## Todo\n- launch\n",
            encoding="utf-8")
        (self.v / "Maria.md").write_text("# Maria\nDesigner. #person\n", encoding="utf-8")
        (self.v / ".obsidian").mkdir()
        (self.v / ".obsidian" / "skip.md").write_text("hidden", encoding="utf-8")
        self.b = Brain(self.v)

    def tearDown(self):
        self.b.db.close()
        self.tmp.cleanup()

    def test_index_and_search(self):
        s = self.b.sync(force=True)
        self.assertEqual(s["notes"], 2)  # .obsidian ignored
        hits = self.b.search("astr")  # prefix match
        self.assertEqual(hits[0].path, "Projects/Website.md")
        self.assertEqual(hits[0].title, "Website redesign")
        self.assertEqual([h.path for h in self.b.search("", tag="person")], ["Maria.md"])
        self.assertEqual(self.b.search('"; DROP TABLE notes; --'), [])  # hostile query is harmless

    def test_links_and_read(self):
        self.b.sync(force=True)
        self.assertEqual(self.b.backlinks("Maria.md"), ["Projects/Website.md"])
        r = self.b.read("Website redesign")  # by title
        self.assertEqual(r["path"], "Projects/Website.md")
        self.assertIn("work", r["tags"])
        sec = self.b.read("Website", section="Decisions")["content"]
        self.assertIn("Netlify", sec)
        self.assertNotIn("launch", sec)

    def test_incremental_sync(self):
        self.b.sync(force=True)
        self.assertEqual(self.b.sync(force=True)["changed"], 0)
        time.sleep(0.01)
        (self.v / "Maria.md").write_text("# Maria\nNow a PM.\n", encoding="utf-8")
        os.utime(self.v / "Maria.md", (time.time() + 5, time.time() + 5))
        self.assertEqual(self.b.sync(force=True)["changed"], 1)
        (self.v / "Maria.md").unlink()
        self.assertEqual(self.b.sync(force=True)["removed"], 1)

    def test_write_append_capture_log(self):
        self.b.write("Ideas.md", "# Ideas\n- one", agent="claude")
        self.b.write("Ideas", "- two", agent="codex", mode="append")
        self.assertIn("- one\n\n- two", (self.v / "Ideas.md").read_text(encoding="utf-8"))
        with self.assertRaises(FileExistsError):
            self.b.write("Ideas.md", "x", mode="create")
        self.b.capture("call the bank", agent="gemini")
        acts = self.b.activity(10)
        self.assertEqual([a["agent"] for a in acts[:3]], ["gemini", "codex", "claude"])
        self.assertEqual(self.b.search("bank")[0].path.split("/")[0], "Daily")

    def test_path_safety(self):
        for bad in ("../evil.md", "/../../etc/passwd", ".obsidian/x.md", ".cortex/index"):
            with self.assertRaises(ValueError):
                self.b.write(bad, "x")

    def test_delete_goes_to_trash(self):
        self.b.delete("Maria.md")
        self.assertFalse((self.v / "Maria.md").exists())
        self.assertEqual(len(list((self.v / ".trash").iterdir())), 1)

    def test_extract_section_missing(self):
        with self.assertRaises(KeyError):
            extract_section("# A\ntext", "B")


class McpTest(unittest.TestCase):
    def rpc(self, server, *msgs):
        out = io.StringIO()
        server.serve(io.StringIO("".join(json.dumps(m) + "\n" for m in msgs)), out)
        return [json.loads(l) for l in out.getvalue().splitlines()]

    def test_protocol_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            s = Server(Brain(d))
            self.addCleanup(s.brain.close)
            r = self.rpc(
                s,
                {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                 "params": {"protocolVersion": "2025-06-18", "clientInfo": {"name": "claude-code"}, "capabilities": {}}},
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "brain_write", "arguments": {"path": "Notes/A.md", "content": "# A\nalpha beta"}}},
                {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "brain_search", "arguments": {"query": "alpha"}}},
                {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "brain_read", "arguments": {"path": "nope"}}},
                {"jsonrpc": "2.0", "id": 6, "method": "tools/call", "params": {"name": "brain_context", "arguments": {}}},
                {"jsonrpc": "2.0", "id": 7, "method": "bogus"},
            )
            self.assertEqual(len(r), 7)  # notification got no reply
            self.assertEqual(r[0]["result"]["protocolVersion"], "2025-06-18")
            self.assertEqual(s.agent, "claude")
            self.assertEqual(len(r[1]["result"]["tools"]), 16)
            self.assertIn("created Notes/A.md", r[2]["result"]["content"][0]["text"])
            self.assertIn("Notes/A.md", r[3]["result"]["content"][0]["text"])
            self.assertTrue(r[4]["result"]["isError"])
            self.assertIn("[claude] created Notes/A.md", r[5]["result"]["content"][0]["text"])
            self.assertEqual(r[6]["error"]["code"], -32601)

    def test_real_subprocess(self):
        with tempfile.TemporaryDirectory() as d:
            p = subprocess.run(
                [sys.executable, str(ROOT / "run.py"), "mcp", "--vault", d],
                input=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}) + "\n",
                capture_output=True, text=True, timeout=20)
            self.assertEqual(json.loads(p.stdout)["result"]["tools"][0]["name"], "brain_context")


class WebTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.brain = Brain(cls.tmp.name)
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(cls.brain))
        cls.url = f"http://127.0.0.1:{cls.httpd.server_port}"
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.brain.db.close()
        cls.tmp.cleanup()

    def req(self, path, data=None, header=True, method=None):
        r = urllib.request.Request(self.url + path, data=json.dumps(data).encode() if data is not None else None, method=method)
        r.add_header("Content-Type", "application/json")
        if header:
            r.add_header("X-Cortex", "1")
        with urllib.request.urlopen(r) as res:
            return json.loads(res.read())

    def test_flow(self):
        self.assertIn("Cortex", urllib.request.urlopen(self.url + "/").read().decode())
        self.assertEqual(self.req("/api/note", {"path": "T.md", "content": "# T\nhello"})["action"], "created")
        self.assertEqual(self.req("/api/search?q=hello")[0]["path"], "T.md")
        self.assertEqual(self.req("/api/note?path=T")["title"], "T")
        self.assertEqual(self.req("/api/activity")[0]["agent"], "you")
        self.assertEqual(self.req("/api/note?path=T.md", method="DELETE")["action"], "deleted")

    def test_write_needs_header(self):
        with self.assertRaises(urllib.error.HTTPError) as e:
            self.req("/api/note", {"path": "X.md", "content": "x"}, header=False)
        self.assertEqual(e.exception.code, 403)

    def test_no_file_escape(self):
        with self.assertRaises(urllib.error.HTTPError) as e:
            self.req("/../core.py")
        self.assertEqual(e.exception.code, 404)


if __name__ == "__main__":
    unittest.main()
