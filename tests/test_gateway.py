"""Milestone 1: canonical events, the gateway queue, the append-only archive, adapters.

Test classes are named for what they run against:
  Mocked*  synthetic data in the documented formats (no provider account needed)
  Live*    real data on this machine (Claude Code transcripts in ~/.claude/projects); skipped if absent
"""

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["CORTEX_SEMANTIC"] = "off"
os.environ.setdefault("CORTEX_LOCAL_DIR", tempfile.mkdtemp(prefix="cortex-test-local-"))

from cortex.archive import ANCHOR  # noqa: E402
from cortex.core import Brain  # noqa: E402
from cortex.mcp_server import Server  # noqa: E402
from cortex_gateway import Gateway  # noqa: E402
from cortex_gateway import queue as Q  # noqa: E402
from cortex_gateway.adapters import capabilities, claude_code, file_drop  # noqa: E402
from cortex_gateway.schema import normalize, validate  # noqa: E402

REAL_CLAUDE = Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude")) / "projects"


def ev(i, text=None, sid="s1", provider="core", role=None, **extra):
    return {"provider": provider, "kind": "message", "session": {"id": sid, "title": "Planning the launch"},
            "message": {"id": f"m{i}", "role": role or ("user" if i % 2 == 0 else "assistant"),
                        "text": text if text is not None else f"message number {i} about the launch plan", "ts": f"2026-11-13T10:{i % 60:02d}:00"},
            **extra}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.t = Path(self.tmp.name)
        self.vault = self.t / "v"
        self.local = self.t / "local-a"
        self._env = mock.patch.dict(os.environ, {"CORTEX_LOCAL_DIR": str(self.local)})
        self._env.start()
        self.b = Brain(self.vault)
        self.gw = Gateway(self.b)

    def tearDown(self):
        self.gw.close()
        self.b.close()
        self._env.stop()
        self.tmp.cleanup()

    def archive_files(self):
        return sorted((self.vault / "Conversations").rglob("*.md")) if (self.vault / "Conversations").exists() else []

    def anchors(self):
        return [a for f in self.archive_files() for a in ANCHOR.findall(f.read_text())]


class MockedSchemaTest(unittest.TestCase):
    def test_partial_metadata_gets_defaults(self):
        e = normalize({"provider": "core", "session": {"id": "abc"}, "message": {"text": "hello there"}})
        self.assertEqual(validate(e), [])
        self.assertEqual(e["message"]["role"], "user")
        self.assertTrue(e["session"]["started"])  # falls back to capture time
        self.assertTrue(e["message"]["id"].startswith("h:"))  # identity by content as a last resort

    def test_invalid_reported_not_raised(self):
        for raw in (None, "x", {}, {"provider": "Bad Name", "session": {"id": "a"}, "message": {"text": "x"}},
                    {"provider": "core", "message": {"text": "x"}}, {"provider": "core", "session": {"id": "a"}, "message": {"text": ""}},
                    {"provider": "core", "session": {"id": "a"}, "message": {"text": "x", "role": "wizard"}}):
            self.assertTrue(validate(normalize(raw)), raw)

    def test_ids_are_deterministic_and_content_sensitive(self):
        a, b = normalize(ev(1)), normalize(ev(1))
        self.assertEqual(a["event_id"], b["event_id"])
        c = normalize(ev(1, text="edited text"))
        self.assertEqual(a["message_key"], c["message_key"])
        self.assertNotEqual(a["event_id"], c["event_id"])

    def test_redaction_and_attachment_refs(self):
        e = normalize({**ev(0, text="my key is sk-ant-abcdefghijklmnopqrstuvwxyz123456"),
                       "attachments": [{"name": "plan.pdf", "uri": "javascript:alert(1)", "sha256": "z" * 64, "size": 10},
                                       {"name": "pic.png", "uri": "/home/u/pic.png", "sha256": "a" * 64}]})
        self.assertNotIn("sk-ant-abc", e["message"]["text"])
        self.assertTrue(e["privacy"]["redacted"])
        self.assertEqual([a["uri"] for a in e["attachments"]], ["", "/home/u/pic.png"])
        self.assertEqual(e["attachments"][0]["sha256"], "")

    def test_capabilities_declared(self):
        caps = {c["provider"]: c for c in capabilities()}
        self.assertEqual(caps["claude-code"]["tested"], "live")
        self.assertIn("hook", caps["claude-code"]["modes"])


class MockedArchiveTest(Base):
    def test_duplicates_three_times_two_devices(self):
        events = [ev(i) for i in range(5)]
        for _ in range(3):
            self.gw.ingest_now(events)
        # a second device: same vault (synced), its own local state
        with mock.patch.dict(os.environ, {"CORTEX_LOCAL_DIR": str(self.t / "local-b")}), mock.patch("cortex.core.DEVICE", "B"):
            b2 = Brain(self.vault)
            g2 = Gateway(b2)
            res = g2.ingest_now(events)
            g2.close()
            b2.close()
        self.assertEqual({r["status"] for r in res}, {"duplicate"})
        self.assertEqual(len(self.archive_files()), 1)
        self.assertEqual(len(self.anchors()), 5)

    def test_append_only_and_revision(self):
        self.gw.ingest_now([ev(0), ev(1)])
        f = self.archive_files()[0]
        before = f.read_bytes()
        r = self.gw.ingest_now([ev(1, text="regenerated answer"), ev(2)])
        self.assertTrue(r[0].get("revision"))
        after = f.read_bytes()
        self.assertTrue(after.startswith(before))  # nothing above was touched
        self.assertIn("(revised)", after.decode())
        self.assertIn("message number 1", after.decode())  # the original stays

    def test_message_text_cannot_forge_anchor(self):
        fake = "<!-- m:aaaaaaaaaaaa h:bbbbbbbb -->"
        self.gw.ingest_now([ev(0, text=f"look {fake}")])
        self.assertNotIn(fake, self.archive_files()[0].read_text())

    def test_legacy_file_adopted_not_rewritten(self):
        from cortex.archive import legacy_ident
        d = self.vault / "Conversations" / "Claude Code" / "2026-11"
        d.mkdir(parents=True)
        legacy = d / f"2026-11-13 1000 Old chat ({legacy_ident('claude-code', 'sess-legacy-123456')}).md"
        legacy.write_text("---\nsource: Claude Code\nsession: sess-legacy-123456\n---\n# Old chat\n\n**You**\n\nhow do I deploy\n\n**Claude**\n\nuse the deploy script\n")
        before = legacy.read_bytes()
        res = self.gw.ingest_now([ev(0, "how do I deploy", sid="sess-legacy-123456", provider="claude-code"),
                                  ev(1, "a brand new reply", sid="sess-legacy-123456", provider="claude-code")])
        self.assertEqual([r["status"] for r in res], ["duplicate", "archived"])
        self.assertTrue(legacy.read_bytes().startswith(before))
        self.assertEqual(len(self.archive_files()), 1)

    def test_importer_never_overwrites_gateway_files(self):
        from cortex.importers import Importer
        self.gw.ingest_now([ev(0, sid="abc", provider="claude-code")])
        f = self.archive_files()[0]
        content = f.read_text()
        imp = Importer(self.b)
        rel = f.relative_to(self.vault).as_posix()
        state = {"notes": {"claude-code:abc": {"sig": "old", "path": rel}}}
        imp._save(state)
        from cortex.importers import Conversation
        c = Conversation("Claude Code", "claude-code:abc", "new")
        c.add("user", "totally different")
        with mock.patch.object(Importer, "_local_conversations", lambda self, st, src: iter([c])):
            imp.run()
        self.assertEqual(f.read_text(), content)

    def test_retrieval_with_attribution(self):
        self.gw.ingest_now([ev(0, "what database should the zebra project use", provider="claude-code", sid="z1"),
                            ev(1, "use sqlite with fts5 for the zebra project", provider="claude-code", sid="z1")])
        hits = self.b.search("zebra sqlite", scope="conversations")
        self.assertTrue(hits and hits[0].path.startswith("Conversations/Claude Code/"))
        self.assertEqual(self.b.search("zebra sqlite", scope="notes"), [])
        rec = self.b.recall("zebra project database", 400)
        self.assertTrue(rec["passages"][0]["path"].startswith("Conversations/Claude Code/"))

    def test_mcp_tool(self):
        s = Server(self.b, "core")
        r = s.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "brain_ingest_events",
                      "arguments": {"events": [ev(0), ev(1), {"provider": "core", "message": {"text": "x"}}, ev(0)]}}})["result"]
        sc = r["structuredContent"]
        self.assertEqual([x["status"] for x in sc["results"]], ["archived", "archived", "invalid", "duplicate"])
        self.assertIn("session.id", " ".join(sc["results"][2]["reasons"]))
        self.assertEqual(self.gw.queue.status()["invalid"], 1)  # kept for inspection, not dropped


class MockedQueueTest(Base):
    def test_retry_backoff_then_dead_then_revive(self):
        self.gw.submit([ev(0)])
        boom = mock.Mock(side_effect=OSError("disk full"))
        t = time.time()
        for i in range(Q.MAX_ATTEMPTS):
            self.gw.queue.drain(boom, now=t + 10_000 * (i + 1))
        st = self.gw.queue.status()
        self.assertEqual((st["pending"], st["dead"]), (0, 1))
        self.assertIn("disk full", st["dead_letters"][0]["error"])
        self.assertEqual(self.gw.queue.retry(dead=True), 1)
        self.assertEqual(self.gw.run()["delivered"], 1)
        self.assertEqual(len(self.anchors()), 1)

    def test_backoff_waits(self):
        self.gw.submit([ev(0)])
        self.gw.queue.drain(mock.Mock(side_effect=OSError("busy")))
        self.assertEqual(self.gw.queue.drain(self.gw.deliver)["delivered"], 0)  # not due yet
        self.gw.queue.retry()
        self.assertEqual(self.gw.queue.drain(self.gw.deliver)["delivered"], 1)

    def test_restart_mid_delivery_exactly_once(self):
        self.gw.submit([ev(i) for i in range(100)])
        calls = {"n": 0}
        real = self.gw.deliver

        def flaky(evs):
            calls["n"] += 1
            if calls["n"] == 3:
                raise RuntimeError("process killed")
            return real(evs)
        self.gw.queue.drain(flaky, batch=10)
        self.gw.close()
        self.gw = Gateway(self.b)  # "restart": a new process opens the same queue
        self.assertEqual(self.gw.queue.status()["pending"], 80)
        self.gw.queue.retry()
        self.gw.run()
        self.assertEqual(self.gw.queue.status()["delivered"], 100)
        self.assertEqual(len(self.anchors()), 100)
        self.assertEqual(len(set(self.anchors())), 100)

    def test_concurrent_writers_same_session(self):
        def worker(start):
            b = Brain(self.vault)
            g = Gateway(b)
            g.ingest_now([ev(i) for i in range(start, start + 20)])
            g.close()
            b.close()
        ths = [threading.Thread(target=worker, args=(k * 20,)) for k in range(4)]
        [t.start() for t in ths]
        [t.join() for t in ths]
        procs = [subprocess.run([sys.executable, "-c", (
            "import sys,os; sys.path.insert(0, %r); os.environ['CORTEX_SEMANTIC']='off';"
            "from cortex.core import Brain; from cortex_gateway import Gateway; from tests.test_gateway import ev;"
            "b=Brain(%r); g=Gateway(b); g.ingest_now([ev(i) for i in range(%d, %d)]); g.close(); b.close()") % (
            str(ROOT), str(self.vault), 80 + k * 10, 90 + k * 10)], env={**os.environ, "CORTEX_LOCAL_DIR": str(self.local)},
            cwd=str(ROOT), capture_output=True, text=True, timeout=60) for k in range(2)]
        for p in procs:
            self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(len(self.archive_files()), 1)
        self.assertEqual(len(set(self.anchors())), 100)
        self.assertEqual(len(self.anchors()), 100)


class MockedClaudeCodeTest(Base):
    def setUp(self):
        super().setUp()
        self.cc = self.t / "claude"
        (self.cc / "projects" / "-home-u-app").mkdir(parents=True)
        self.tp = self.cc / "projects" / "-home-u-app" / "sess-1.jsonl"
        self._cc = mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.cc)})
        self._cc.start()

    def tearDown(self):
        self._cc.stop()
        super().tearDown()

    def line(self, i, typ, text, **kw):
        content = text if typ == "user" else [{"type": "text", "text": text}]
        return json.dumps({"type": typ, "uuid": f"u{i}", "sessionId": "sess-1", "timestamp": f"2026-11-13T09:{i:02d}:00Z",
                           "cwd": "/home/u/app", "message": {"role": typ, "content": content}, **kw}) + "\n"

    def test_scan_incremental_and_partial_line(self):
        self.tp.write_text(self.line(0, "user", "set up the build") + self.line(1, "assistant", "run npm ci first")
                           + self.line(2, "assistant", "sidechain", isSidechain=True) + '{"type":"user","uuid":"u9"')
        r = claude_code.scan(self.gw)
        self.assertEqual(r["queued"], 2)
        self.gw.run()
        self.assertEqual(claude_code.scan(self.gw)["queued"], 0)  # unchanged file: nothing re-read
        with open(self.tp, "a") as f:  # finish the half-written line, add one more
            f.write(', "sessionId":"sess-1","timestamp":"2026-11-13T09:09:00Z","message":{"role":"user","content":"and the tests?"}}\n')
            f.write(self.line(10, "assistant", "npm test"))
        self.assertEqual(claude_code.scan(self.gw)["queued"], 2)
        self.gw.run()
        text = self.archive_files()[0].read_text()
        self.assertIn("provider: claude-code", text)
        self.assertEqual(len(self.anchors()), 4)
        self.assertNotIn("sidechain", text)

    def test_hook_reads_only_inside_projects(self):
        self.tp.write_text(self.line(0, "user", "hello from the hook"))
        self.assertEqual(claude_code.hook(self.gw, {"transcript_path": str(self.tp), "hook_event_name": "Stop"})["queued"], 1)
        outside = self.t / "evil.jsonl"
        outside.write_text(self.line(0, "user", "x"))
        for bad in ({"transcript_path": str(outside)}, {"transcript_path": "/etc/passwd"}, {}):
            with self.assertRaises(ValueError):
                claude_code.hook(self.gw, bad)

    def test_cli_respects_consent(self):
        from cortex_gateway.cli import run
        self.tp.write_text(self.line(0, "user", "consent check"))
        with mock.patch("cortex.cli.import_allowed", return_value=False):
            self.assertIn("skipped", run(self.vault, "scan"))
        with mock.patch("cortex.cli.import_allowed", return_value=True):
            r = run(self.vault, "scan")
        self.assertEqual(r["delivery"]["delivered"], 1)


class MockedFileDropTest(Base):
    def test_valid_and_invalid_lines(self):
        d = self.vault / ".cortex" / "drop"
        d.mkdir(parents=True, exist_ok=True)
        (d / "batch.jsonl").write_text(json.dumps(ev(0)) + "\nnot json\n" + json.dumps({"provider": "core"}) + "\n" + json.dumps(ev(1)) + "\n")
        r = file_drop.process(self.gw)
        self.assertEqual((r["queued"], r["invalid"]), (2, 2))
        self.assertTrue((d / "failed" / "batch.jsonl").exists())
        errs = json.loads((d / "failed" / "batch.errors.json").read_text())
        self.assertEqual(sorted(e["line"] for e in errs), [2, 3])
        self.gw.run()
        self.assertEqual(len(self.anchors()), 2)


@unittest.skipUnless(REAL_CLAUDE.is_dir() and list(REAL_CLAUDE.glob("*/*.jsonl")), "no Claude Code transcripts on this machine")
class LiveClaudeCodeTest(Base):
    """Runs against the real Claude Code transcripts on this machine (read-only)."""

    def test_real_transcripts(self):
        files = claude_code.transcripts()
        r = claude_code.scan(self.gw)
        self.assertGreater(r["queued"], 0, r)
        d = self.gw.run()
        self.assertEqual(d["failed"], 0)
        self.assertGreaterEqual(len(self.archive_files()), 1)
        again = claude_code.scan(self.gw)["queued"]  # second pass: only lines written since (this very session is live)
        self.assertLess(again, r["queued"])
        # every archived note says where it came from
        for f in self.archive_files():
            head = f.read_text(encoding="utf-8")[:600]
            self.assertIn("provider: claude-code", head)
            self.assertIn("session: ", head)
        # cross-provider retrieval: a phrase from a real transcript is found by search
        with open(files[0], encoding="utf-8") as fh:
            lines = [json.loads(l) for l in fh if l.strip()]
        sample = next(m for m in lines
                      if m.get("type") == "user" and isinstance((m.get("message") or {}).get("content"), str)
                      and len(m["message"]["content"].split()) > 3 and "<" not in m["message"]["content"])
        words = " ".join(sample["message"]["content"].split()[:6])
        self.assertTrue(self.b.search(words, scope="conversations"), words)

    def test_real_hook_payload(self):
        tp = claude_code.transcripts()[0]
        r = claude_code.hook(self.gw, {"session_id": tp.stem, "transcript_path": str(tp), "hook_event_name": "Stop"})
        self.assertGreater(r["read"], 0)
        self.gw.run()
        self.assertLess(claude_code.hook(self.gw, {"transcript_path": str(tp)})["queued"], r["read"])


if __name__ == "__main__":
    unittest.main()
