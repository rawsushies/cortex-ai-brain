"""Milestone 2: every provider through the gateway, REST intake, capture rules, forget, retention.

Mocked* = synthetic data in documented formats. Live* = real data on this machine (skipped if absent).
"""

import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
import zipfile
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["CORTEX_SEMANTIC"] = "off"
os.environ.setdefault("CORTEX_LOCAL_DIR", tempfile.mkdtemp(prefix="cortex-test-local-"))

from cortex import importers as I  # noqa: E402
from cortex.archive import ANCHOR, forget  # noqa: E402
from cortex.core import Brain  # noqa: E402
from cortex_gateway import Gateway, rules  # noqa: E402
from cortex_gateway.adapters import capabilities, claude_code, exports  # noqa: E402
from cortex_gateway.cli import purge_queue, retain  # noqa: E402

REAL_CLAUDE = Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude")) / "projects"


def ev(i, sid="s1", provider="core", project="", started="2026-11-13T10:00:00", text=None):
    return {"provider": provider, "kind": "message", "session": {"id": sid, "title": "A chat", "project": project, "started": started},
            "message": {"id": f"m{i}", "role": "user" if i % 2 == 0 else "assistant", "text": text or f"line {i} of the chat", "ts": started}}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.t = Path(self.tmp.name)
        self.vault = self.t / "v"
        self._env = mock.patch.dict(os.environ, {"CORTEX_LOCAL_DIR": str(self.t / "local-a")})
        self._env.start()
        self.b = Brain(self.vault)
        self.gw = Gateway(self.b)

    def tearDown(self):
        self.gw.close()
        self.b.close()
        self._env.stop()
        self.tmp.cleanup()

    def files(self):
        d = self.vault / "Conversations"
        return sorted(d.rglob("*.md")) if d.exists() else []


class MockedAdaptersTest(Base):
    def test_all_providers_declare_capabilities(self):
        caps = {c["provider"]: c for c in capabilities()}
        self.assertEqual(set(caps), {"claude-code", "codex", "gemini-cli", "cursor", "chatgpt", "claude-ai", "gemini", "*"})
        for p in ("chatgpt", "claude-ai", "gemini"):
            self.assertEqual(list(caps[p]["modes"]), ["export"])
            self.assertTrue(caps[p]["realtime"].startswith("no"))
        self.assertEqual(caps["claude-code"]["tested"], "live")
        self.assertTrue(all(c["tested"] == "fixture" for p, c in caps.items() if p not in ("claude-code", "*")))

    def test_gemini_takeout_zip(self):
        activity = [
            {"header": "Gemini Apps", "title": "Prompted How do I bake sourdough bread", "time": "2026-10-01T08:00:00.000Z",
             "products": ["Gemini Apps"], "safeHtmlItem": [{"html": "<p>Start with an active <b>starter</b>.</p>"}]},
            {"header": "Gemini Apps", "title": "Prompted Best hydration for sourdough", "time": "2026-10-01T09:00:00.000Z"},
            {"header": "Gemini Apps", "title": "Prompted Plan a garden", "time": "2026-10-02T09:00:00.000Z"},
            {"header": "Search", "title": "Searched for cats", "time": "2026-10-02T09:00:00.000Z"}]
        z = self.t / "takeout.zip"
        with zipfile.ZipFile(z, "w") as f:
            f.writestr("Takeout/My Activity/Gemini Apps/MyActivity.json", json.dumps(activity))
        r = I.Importer(self.b).run(export=z)
        self.assertEqual(r["by_source"], {"Gemini": 2})  # grouped by day
        text = "\n".join(p.read_text() for p in self.files())
        self.assertIn("How do I bake sourdough bread", text)
        self.assertIn("Start with an active starter.", text)
        self.assertNotIn("cats", text)
        self.assertEqual(I.Importer(self.b).run(export=z)["new"], 0)

    def test_chatgpt_reexport_with_regenerated_reply_is_a_revision(self):
        def export(answer):
            return [{"conversation_id": "c1", "title": "Trip", "create_time": 1762000000, "update_time": 1762000100,
                     "current_node": "n2", "mapping": {
                         "n1": {"parent": None, "message": {"author": {"role": "user"}, "create_time": 1762000000,
                                                            "content": {"content_type": "text", "parts": ["Plan a trip"]}}},
                         "n2": {"parent": "n1", "message": {"author": {"role": "assistant"}, "create_time": 1762000050,
                                                            "content": {"content_type": "text", "parts": [answer]}}}}}]
        f = self.t / "conversations.json"
        f.write_text(json.dumps(export("Go to the coast")))
        I.Importer(self.b).run(export=f)
        before = self.files()[0].read_bytes()
        f.write_text(json.dumps(export("Go to the mountains")))
        r = I.Importer(self.b).run(export=f)
        after = self.files()[0].read_bytes()
        self.assertEqual(r["updated"], 1)
        self.assertTrue(after.startswith(before))
        self.assertIn(b"(revised)", after)

    def test_one_capture_path_for_claude_code(self):
        home = self.t / "home"
        tp = home / ".claude/projects/-p/sess9.jsonl"
        tp.parent.mkdir(parents=True)
        tp.write_text("\n".join(json.dumps({"type": t, "uuid": f"u{i}", "timestamp": "2026-11-01T09:00:0%dZ" % i,
                                            "message": {"role": t, "content": f"hello {i}" if t == "user" else [{"type": "text", "text": f"reply {i}"}]}})
                                for i, t in enumerate(["user", "assistant"])) + "\n")
        with mock.patch.object(I, "HOME", home), mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(home / ".claude")}):
            claude_code.scan(self.gw)  # the hook / scan path
            self.gw.run()
            r = I.Importer(self.b).run()  # the app's importer path
        self.assertEqual((r["new"], r["updated"]), (0, 0))
        self.assertEqual(len(ANCHOR.findall(self.files()[0].read_text())), 2)


class MockedRulesTest(Base):
    def test_provider_off_project_excluded_extra_redaction(self):
        rules.save(self.vault, {"providers": {"chatgpt": {"enabled": False}},
                                "projects": {"exclude": ["*/secret*"]}, "redact": [r"PROJ-\d{4}"]})
        res = self.gw.ingest_now([ev(0, provider="chatgpt"), ev(1, project="/home/u/secret-app"),
                                  ev(2, text="ticket PROJ-1234 is done")])
        self.assertEqual([r["status"] for r in res], ["skipped", "skipped", "archived"])
        self.assertEqual(res[0]["reason"], "capture from chatgpt is turned off")
        text = self.files()[0].read_text()
        self.assertNotIn("PROJ-1234", text)
        self.assertEqual(self.gw.queue.status()["pending"] + self.gw.queue.status()["delivered"], 1)  # skipped never queued

    def test_bad_rules_rejected(self):
        for bad in ({"providers": {"Bad Name": {}}}, {"providers": {"x": {"retention_days": -1}}}, {"redact": ["("]},
                    {"providers": []}):
            with self.assertRaises(ValueError):
                rules.save(self.vault, bad)


class MockedForgetTest(Base):
    def test_forget_session_survives_rescan_and_second_device(self):
        events = [ev(i, sid="private") for i in range(3)] + [ev(i, sid="keep") for i in range(2)]
        self.gw.ingest_now(events)
        self.b.write(self.files()[0].relative_to(self.vault).as_posix(), "x", agent="you", mode="append")  # makes a history copy
        r = forget(self.b, provider="core", session="private")
        self.assertEqual(r["count"], 1)
        purge_queue(self.gw, "core", "private")
        self.assertEqual(len(self.files()), 1)
        self.assertFalse(any("private" in p.as_posix() for p in (self.vault / ".cortex" / "history").rglob("*")))
        self.assertEqual({x["status"] for x in self.gw.ingest_now(events[:3])}, {"skipped"})  # re-capture: refused
        with mock.patch.dict(os.environ, {"CORTEX_LOCAL_DIR": str(self.t / "local-b")}), mock.patch("cortex.core.DEVICE", "B"):
            b2 = Brain(self.vault)
            g2 = Gateway(b2)
            self.assertEqual({x["status"] for x in g2.ingest_now(events[:3])}, {"skipped"})
            g2.close()
            b2.close()
        self.assertEqual(len(self.files()), 1)
        self.assertEqual(self.b.search("line 1 of the chat", scope="conversations")[0].path, self.files()[0].relative_to(self.vault).as_posix())

    def test_retention(self):
        old = [ev(i, sid="old", started="2020-01-01T10:00:00") for i in range(2)]
        new = [ev(i, sid="new", started="2099-01-01T10:00:00") for i in range(2)]
        self.gw.ingest_now(old + new)
        rules.save(self.vault, {"providers": {"core": {"retention_days": 30}}})
        r = retain(self.b)
        self.assertEqual(r["forgot"], {"core": 1})
        self.assertEqual(len(self.files()), 1)
        self.assertEqual({x["status"] for x in self.gw.ingest_now(old)}, {"skipped"})

    def test_forget_needs_a_target(self):
        with self.assertRaises(ValueError):
            forget(self.b)
        with self.assertRaises(ValueError):
            forget(self.b, before="last tuesday")


class MockedRestTest(Base):
    def setUp(self):
        super().setUp()
        from cortex.web import make_handler
        self.cfg = self.t / "config.json"
        self._cfg = mock.patch("cortex.cli.CONFIG", self.cfg)
        self._cfg.start()
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.b))
        self.port = self.httpd.server_port
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self._cfg.stop()
        super().tearDown()

    def post(self, data, headers=None):
        r = urllib.request.Request(f"http://127.0.0.1:{self.port}/api/events", data=json.dumps(data).encode())
        for k, v in {"Content-Type": "application/json", **(headers or {})}.items():
            r.add_header(k, v)
        try:
            with urllib.request.urlopen(r) as res:
                return res.status, json.loads(res.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def test_token_required(self):
        self.assertEqual(self.post({"events": [ev(0)]})[0], 403)  # intake off until a token exists
        from cortex_gateway.cli import token
        tok = token()
        self.assertEqual(self.post({"events": [ev(0)]})[0], 401)
        self.assertEqual(self.post({"events": [ev(0)]}, {"Authorization": "Bearer wrong"})[0], 401)
        self.assertEqual(self.post({"events": [ev(0)]}, {"Authorization": f"Bearer {tok}", "Host": "evil.example"})[0], 403)
        code, body = self.post({"events": [ev(0), ev(1)]}, {"Authorization": f"Bearer {tok}"})
        self.assertEqual((code, body["archived"]), (200, 2))
        code, body = self.post(ev(1), {"Authorization": f"Bearer {tok}"})  # a single event object works too
        self.assertEqual(body["duplicate"], 1)
        payloads = [json.loads(r[0]) for r in self.gw.queue.db.execute("SELECT payload FROM events")]
        self.assertEqual({p["capture"] for p in payloads}, {"rest"})
        self.assertNotEqual(token(new=True), tok)
        self.assertEqual(self.post({"events": [ev(2)]}, {"Authorization": f"Bearer {tok}"})[0], 401)  # old token revoked


@unittest.skipUnless(REAL_CLAUDE.is_dir() and list(REAL_CLAUDE.glob("*/*.jsonl")), "no Claude Code transcripts on this machine")
class LiveImporterTest(Base):
    def test_importer_and_scan_agree_on_real_transcripts(self):
        n = claude_code.scan(self.gw)["queued"]
        self.gw.run()
        anchors = sum(len(ANCHOR.findall(p.read_text(encoding="utf-8"))) for p in self.files())
        with mock.patch.object(I, "HOME", Path.home()):
            I.Importer(self.b).run({"claude-code"})
        after = sum(len(ANCHOR.findall(p.read_text(encoding="utf-8"))) for p in self.files())
        self.assertGreater(n, 0)
        self.assertLess(after - anchors, max(5, n // 20))  # only lines written meanwhile (this session is live)


if __name__ == "__main__":
    unittest.main()
