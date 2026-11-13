"""The 2.0 app API: smart capture, undo, rules, graph, topics. Same guards as every endpoint."""

import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["CORTEX_SEMANTIC"] = "off"
os.environ.setdefault("CORTEX_LOCAL_DIR", tempfile.mkdtemp(prefix="cortex-test-local-"))

from cortex.core import Brain  # noqa: E402
from cortex.web import make_handler  # noqa: E402


class Web2Test(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.b = Brain(Path(self.tmp.name) / "v")
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.b))
        self.port = self.httpd.server_port
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.b.close()
        self.tmp.cleanup()

    def req(self, path, data=None, headers=None):
        r = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=json.dumps(data).encode() if data is not None else None)
        for k, v in {"X-Cortex": "1", "Content-Type": "application/json", **(headers or {})}.items():
            r.add_header(k, v)
        try:
            with urllib.request.urlopen(r) as res:
                return res.status, json.loads(res.read() or b"null")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"null")

    def test_ingest_then_undo(self):
        code, r = self.req("/api/ingest", {"text": "Remind me to call the bank tomorrow"})
        self.assertEqual((code, r["type"]), (200, "task"))
        self.assertEqual(len(self.b.tasks("open")), 1)
        code, u = self.req("/api/undo", {"changeset": r["changeset"]})
        self.assertEqual(code, 200)
        self.assertEqual(len(self.b.tasks("open")), 0)
        code, sets = self.req("/api/changes?limit=5")
        self.assertTrue(sets[0]["undo_of"] == r["changeset"])

    def test_ingest_needs_header_and_text(self):
        self.assertEqual(self.req("/api/ingest", {"text": "x y z"}, headers={"X-Cortex": "0"})[0], 403)
        self.assertEqual(self.req("/api/ingest", {"text": 5})[0], 400)
        self.assertEqual(self.req("/api/ingest", {"text": "x"}, headers={"Host": "evil.example"})[0], 403)

    def test_rules_validated_and_applied(self):
        self.assertEqual(self.req("/api/rules", {"rules": [{"match": {"regex": "("}, "category": "X"}]})[0], 400)
        self.assertEqual(self.req("/api/rules", {"rules": [{"match": {}, "category": ""}]})[0], 400)
        code, out = self.req("/api/rules", {"rules": [{"match": {"keywords": ["zebra"]}, "category": "Zoo"}],
                                            "settings": {"auto_file": True}})
        self.assertEqual(code, 200)
        self.assertTrue(out["settings"]["auto_file"])
        self.b.write("Z.md", "# Z\na zebra note", agent="you")
        self.assertEqual(self.req("/api/categories")[1], [{"category": "Zoo", "count": 1}])
        self.assertEqual(self.req("/api/rules")[1]["rules"][0]["category"], "Zoo")

    def test_graph_and_topics(self):
        self.b.write("A.md", "# A\n[[B]] [[Nowhere]]", agent="you")
        self.b.write("B.md", "# B\nx", agent="you")
        code, g = self.req("/api/graph2?metric=pagerank")
        self.assertEqual(code, 200)
        ids = {n["id"] for n in g["nodes"]}
        self.assertTrue({"A.md", "B.md", "?Nowhere"} <= ids)
        self.assertEqual(self.req("/api/graph2?metric=bogus")[0], 400)
        self.assertEqual(self.req("/api/topics")[1], [])
        self.assertEqual(self.req("/api/topics/refresh", {})[0], 200)

    def test_fonts_and_mark_served(self):
        for path, ctype in (("/fonts/inter-var.woff2", "font/woff2"), ("/mark.svg", "image/svg+xml")):
            r = urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}")
            self.assertEqual(r.headers["Content-Type"], ctype)


if __name__ == "__main__":
    unittest.main()
