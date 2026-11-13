"""Milestone 3: unverified knowledge waits for review; conversation, project and about tools; sync guard.
All mocked (synthetic data); see test_gateway*.py for live tests."""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["CORTEX_SEMANTIC"] = "off"
os.environ.setdefault("CORTEX_LOCAL_DIR", tempfile.mkdtemp(prefix="cortex-test-local-"))

from cortex import cloud  # noqa: E402
from cortex.core import Brain  # noqa: E402
from cortex.mcp_server import Server  # noqa: E402
from cortex_gateway import Gateway  # noqa: E402


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.t = Path(self.tmp.name)
        self._env = mock.patch.dict(os.environ, {"CORTEX_LOCAL_DIR": str(self.t / "local")})
        self._env.start()
        self.vault = self.t / "v"
        self.b = Brain(self.vault)
        self.s = Server(self.b, "core")

    def tearDown(self):
        self.b.close()
        self._env.stop()
        self.tmp.cleanup()

    def call(self, tool, **args):
        r = self.s.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": tool, "arguments": args}})["result"]
        if r.get("isError"):
            raise AssertionError(r["content"][0]["text"])
        return r["structuredContent"]


class MockedVerificationTest(Base):
    def setUp(self):
        super().setUp()
        self.b.write("Projects/Website.md", "# Website\nThe site.", agent="you")

    def test_model_inference_goes_to_review_not_knowledge(self):
        r = self.call("brain_ingest", text="We decided to host the Website on a VPS; need to renew the domain by Friday",
                      source="model", ref="Conversations/COre/x.md")
        self.assertEqual((r["action"], r["path"], r["verified"]), ("review", "Inbox/Review.md", False))
        self.assertTrue(r["tasks"][0]["proposed"])
        self.assertEqual(self.b.tasks("open"), [])  # proposals are not on the task list
        self.assertNotIn("VPS", (self.vault / "Projects/Website.md").read_text())
        rec = self.b.records("decision")[0]
        self.assertEqual((rec["verified"], rec["ref"]), (False, "Conversations/COre/x.md"))

    def test_what_the_user_said_is_verified(self):
        r = self.call("brain_ingest", text="We decided to use Postgres for the Website", source="user")
        self.assertEqual((r["action"], r["verified"]), ("stored", True))
        self.assertIn("Postgres", (self.vault / "Projects/Website.md").read_text())

    def test_recall_ranks_verified_first_and_flags(self):
        self.call("brain_ingest", text="The Website deadline is March 3 according to the client email", source="email")
        self.b.write("Website deadline.md", "# Website deadline\nThe Website deadline is March 10, confirmed by the client.", agent="you")
        p = self.b.recall("website deadline", 600)["passages"]
        self.assertTrue(p[0]["verified"])
        self.assertIn(False, [x["verified"] for x in p])

    def test_accept_reject_and_undo(self):
        self.call("brain_ingest", text="We decided to add dark mode to the Website", source="model", project="Website")
        self.call("brain_ingest", text="remind me to email the designer tomorrow", source="model")
        self.call("brain_ingest", text="Idea: a weekly digest of Website analytics", source="model")
        items = self.call("brain_verify")["items"]
        self.assertEqual(len(items), 3)
        r = self.call("brain_verify", action="accept", path="Inbox/Review.md", text="dark mode")
        self.assertEqual(r["filed_in"], "Projects/Website.md")
        line = next(l for l in (self.vault / "Projects/Website.md").read_text().splitlines() if "dark mode" in l)
        self.assertIn("[by:: core]", line)  # provenance kept
        self.assertIn("[verified:: true]", line)
        self.call("brain_verify", action="accept", path="Inbox/Review.md", text="email the designer")
        self.assertEqual([t["text"].split(" 📅")[0] for t in self.b.tasks("open")], ["Email the designer"])
        r = self.call("brain_verify", action="reject", path="Inbox/Review.md", text="weekly digest")
        self.assertEqual(self.call("brain_verify")["items"], [])
        self.call("brain_undo", changeset=r["changeset"])  # changed my mind
        self.assertEqual(len(self.call("brain_verify")["items"]), 1)

    def test_long_unverified_note(self):
        long = "Research summary about hosting options.\n\n" + "Shared hosting is cheap but slow. " * 30
        r = self.call("brain_ingest", text=long, source="web", ref="https://example.com/hosting")
        self.assertTrue(r["path"].startswith("Inbox/"))
        note = (self.vault / r["path"]).read_text()
        self.assertIn("verified: false", note)
        self.assertIn("trust: external", note)
        self.call("brain_verify", action="accept", path=r["path"])
        self.assertIn("verified: true", (self.vault / r["path"]).read_text())
        self.assertEqual(self.call("brain_verify")["items"], [])


class MockedRetrievalToolsTest(Base):
    def setUp(self):
        super().setUp()
        gw = Gateway(self.b)
        ev = lambda prov, sid, i, text: {"provider": prov, "kind": "message", "session": {"id": sid, "title": f"{prov} chat", "project": "/home/u/website"},
                                         "message": {"id": f"m{i}", "role": "user" if i % 2 == 0 else "assistant", "text": text, "ts": f"2026-11-1{i}T10:00:00"}}
        gw.ingest_now([ev("claude-code", "cc1", 0, "How should the Website handle login?"), ev("claude-code", "cc1", 1, "Use sessions with secure cookies for the Website."),
                       ev("chatgpt", "gp1", 2, "Website colour palette ideas please"), ev("chatgpt", "gp1", 3, "Try teal and slate for the Website.")])
        gw.close()
        self.b.write("Projects/Website.md", "# Website\nSee [[Login]].\n## Decisions\n- 2026-11-01 We decided to use sessions [type:: decision]\n\n## Tasks\n- [ ] Ship login 📅 2026-12-01\n", agent="you")
        self.b.write("Login.md", "# Login\nNotes on login.", agent="you")

    def test_conversation_list_and_read(self):
        rows = self.call("brain_conversation")["conversations"]
        self.assertEqual({r["provider"] for r in rows}, {"claude-code", "chatgpt"})
        only = self.call("brain_conversation", provider="chatgpt")["conversations"]
        self.assertEqual([r["session"] for r in only], ["gp1"])
        r = self.call("brain_conversation", session="cc1")
        self.assertEqual((r["provider"], r["session"]), ("claude-code", "cc1"))
        self.assertIn("secure cookies", r["content"])
        found = self.call("brain_conversation", query="colour palette")["conversations"]
        self.assertEqual(found[0]["provider"], "chatgpt")

    def test_project_context(self):
        r = self.call("brain_project", name="Website", budget=800)
        self.assertEqual(r["path"], "Projects/Website.md")
        self.assertEqual(r["decisions"][0]["text"], "2026-11-01 We decided to use sessions")
        self.assertEqual(r["tasks"][0]["text"].split(" 📅")[0], "Ship login")
        self.assertIn("Login.md", r["linked"])
        self.assertTrue({c["provider"] for c in r["conversations"]} & {"claude-code", "chatgpt"})
        self.assertLessEqual(r["tokens"], 800)

    def test_about(self):
        r = self.call("brain_about")
        self.assertEqual(r["vault"], str(self.vault.resolve()))
        self.assertEqual(r["gateway"]["delivered"], 4)
        self.assertIn("claude-code", r["adapters"])
        self.assertIn("about", r["features"])


class MockedSyncGuardTest(Base):
    def test_two_providers_warned(self):
        (self.vault / ".stfolder").mkdir()
        with mock.patch.object(cloud, "provider_for", return_value={"provider": "Google Drive", "path": "G:/My Drive"}):
            self.assertEqual([p["provider"] for p in cloud.sync_providers(self.vault)], ["Syncthing", "Google Drive"])
            r = self.call("brain_health")
        self.assertIn("sync", r["summary"])
        self.assertEqual(len(r["sync_providers"]), 2)

    def test_one_provider_is_fine(self):
        (self.vault / ".stfolder").mkdir()
        with mock.patch.object(cloud, "provider_for", return_value=None):
            r = self.call("brain_health")
        self.assertNotIn("sync", r["summary"])

    def test_cloud_move_refused_when_syncthing_runs(self):
        from cortex import cli
        (self.vault / ".stfolder").mkdir()
        out = []
        with mock.patch.object(cli, "vault_path", return_value=self.vault), mock.patch.object(cloud, "provider_for", return_value=None), \
                mock.patch("builtins.print", lambda *a, **k: out.append(" ".join(map(str, a)))), \
                mock.patch.object(cloud, "move_vault") as mv:
            cli.cmd_cloud(mock.Mock(vault=None, to=None, yes=True))
        mv.assert_not_called()
        self.assertIn("already syncs with Syncthing", "\n".join(out))


if __name__ == "__main__":
    unittest.main()
