"""Live: two real Syncthing peers on ONE machine (two homes, two ports, the real sync protocol over
127.0.0.1), each with its own vault copy, its own Cortex index and its own device name. This tests the
multi-device code for real; it is not a test on two physical computers.

Skipped when the `syncthing` binary is missing; the Linux CI job installs it and sets
CORTEX_REQUIRE_SYNCTHING=1 so the tests can never be skipped silently there."""

import os
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
os.environ["CORTEX_SEMANTIC"] = "off"
os.environ.setdefault("CORTEX_LOCAL_DIR", tempfile.mkdtemp(prefix="cortex-test-local-"))

import syncpair  # noqa: E402
from cortex import archive, cloud, core  # noqa: E402
from cortex.core import Brain  # noqa: E402
from cortex.organize import Organizer  # noqa: E402
from cortex_gateway import Gateway  # noqa: E402


def chat(sid: str, n: int = 4, title: str = "Pair test chat"):
    return [{"provider": "claude-code", "kind": "message",
             "session": {"id": sid, "title": title, "project": "/home/u/garden", "started": "2026-11-10T09:00:00"},
             "message": {"id": f"{sid}-m{i}", "role": "user" if i % 2 == 0 else "assistant",
                         "text": f"Message {i} about the compost bin in session {sid}.", "ts": f"2026-11-10T09:0{i}:00"}}
            for i in range(n)]


if os.environ.get("CORTEX_REQUIRE_SYNCTHING") and not syncpair.available():
    raise RuntimeError("CORTEX_REQUIRE_SYNCTHING is set but syncthing is not installed")


@unittest.skipUnless(syncpair.available(), "syncthing is not installed")
class SyncthingPairTest(unittest.TestCase):
    """One pair for the whole class (start-up is the slow part); each test uses its own notes."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        t = Path(cls.tmp.name)
        cls.env = mock.patch.dict(os.environ, {"CORTEX_LOCAL_DIR": str(t / "local")})
        cls.env.start()
        cls.pair = syncpair.SyncPair(t).__enter__()
        cls.A = Brain(cls.pair.a)
        with cls.device("peer-b"):
            cls.B = Brain(cls.pair.b)

    @classmethod
    def tearDownClass(cls):
        cls.A.close()
        cls.B.close()
        cls.pair.__exit__(None, None, None)
        cls.env.stop()
        cls.tmp.cleanup()

    @staticmethod
    @contextmanager
    def device(name: str):
        """Act as another machine: Cortex names its per-device journals after DEVICE."""
        with mock.patch.object(core, "DEVICE", name):
            yield

    def on_b(self, fn, *a, **k):
        with self.device("peer-b"):
            return fn(*a, **k)

    def wait(self, pred, what):
        self.pair.wait(pred, 60, what)

    # ---- tests -------------------------------------------------------------------------------

    def test_archive_dedup_across_peers(self):
        events = chat("pair-dedup")
        gw = Gateway(self.A)
        r = gw.ingest_now(events)
        gw.close()
        self.assertEqual({x["status"] for x in r}, {"archived"})
        path = next(p for p in (self.pair.a / "Conversations").rglob("*.md") if "pair-dedup" in p.read_text())
        rel = path.relative_to(self.pair.a)
        other = self.pair.b / rel
        self.wait(lambda: other.exists() and other.read_text() == path.read_text(), "the archive note on B")
        before = other.read_bytes()

        def recapture():
            gw = Gateway(self.B)
            try:
                return gw.ingest_now(events)
            finally:
                gw.close()
        self.B.sync(force=True)
        again = self.on_b(recapture)
        self.assertEqual({x["status"] for x in again}, {"duplicate"})  # B saw the anchors A wrote
        self.assertEqual(other.read_bytes(), before)  # and changed nothing
        self.assertEqual(len([p for p in (self.pair.b / "Conversations").rglob("*.md") if "pair-dedup" in p.read_text()]), 1)
        self.B.sync(force=True)
        self.assertTrue(self.on_b(self.B.search, "compost bin pair-dedup", scope="conversations"))

        # each machine writes its own logbook file; both end up on both peers, nothing is merged into one file
        self.on_b(self.B.log, "you", "note", "peer B was here")
        self.A.log("you", "note", "peer A was here")
        for v in (self.pair.a, self.pair.b):
            self.wait(lambda v=v: {"peer-b.jsonl", f"{core.DEVICE}.jsonl"} <= {f.name for f in (v / ".cortex/activity").glob("*.jsonl")},
                      "both logbooks on both peers")

    def test_conflict_copy_is_reported_and_nothing_is_lost(self):
        self.A.write("Plan.md", "# Plan\nStart here.\n", agent="you")
        self.wait(lambda: (self.pair.b / "Plan.md").exists(), "Plan.md on B")
        self.pair.idle()
        self.pair.pause()
        try:
            self.A.write("Plan.md", "# Plan\nStart here.\nEdit from A: buy seeds.\n", agent="you")
            self.on_b(self.B.write, "Plan.md", "# Plan\nStart here.\nEdit from B: fix the fence.\n", agent="you")
        finally:
            self.pair.resume()
        copies = lambda v: list(v.glob("Plan.sync-conflict-*.md"))
        self.wait(lambda: copies(self.pair.a) and copies(self.pair.b), "a sync-conflict copy on both peers")
        self.pair.idle()
        # nothing overwritten: the two edits both survive, one in Plan.md and one in the conflict copy
        texts = (self.pair.b / "Plan.md").read_text() + copies(self.pair.b)[0].read_text()
        self.assertIn("buy seeds", texts)
        self.assertIn("fix the fence", texts)
        self.B.sync(force=True)
        h = self.on_b(self.B.health)
        self.assertEqual([c["original"] for c in h["sync_conflicts"]], ["Plan.md"])
        self.assertLess(h["score"], 100)

    def test_forget_on_a_blocks_capture_on_b(self):
        events = chat("pair-forget")
        gw = Gateway(self.A)
        gw.ingest_now(events)
        gw.close()
        find = lambda v: [p for p in (v / "Conversations").rglob("*.md") if "pair-forget" in p.read_text()]
        self.wait(lambda: find(self.pair.b), "the archive note on B")
        r = archive.forget(self.A, session="pair-forget")
        self.assertEqual(len(r["removed"]), 1)
        stone = self.pair.b / ".cortex/tombstones" / f"{core.DEVICE}.jsonl"
        self.wait(lambda: not find(self.pair.b) and stone.exists() and "pair-forget" in stone.read_text(),
                  "the deletion and the tombstone on B")

        def recapture():  # B still has the transcript locally (say) and tries to capture it again
            gw = Gateway(self.B)
            try:
                return gw.ingest_now(events)
            finally:
                gw.close()
        again = self.on_b(recapture)
        self.assertEqual({x["status"] for x in again}, {"skipped"})
        self.assertEqual(find(self.pair.b), [])

    def test_review_accepted_on_a_is_verified_on_b(self):
        self.A.write("Projects/Garden.md", "# Garden\nThe allotment.\n", agent="you")
        org = Organizer(self.A)
        r = org.ingest("We decided to plant garlic in the Garden before December", agent="core", source="model",
                       ref="Conversations/x.md")
        self.assertEqual((r["action"], r["verified"]), ("review", False))
        garden_b = self.pair.b / "Projects/Garden.md"
        self.wait(lambda: (self.pair.b / "Inbox/Review.md").exists() and garden_b.exists(), "the review item on B")
        self.B.sync(force=True)
        before = [p for p in self.on_b(self.B.recall, "plant garlic garden", 600)["passages"] if "garlic" in p.get("text", "")]
        self.assertTrue(before and not any(p["verified"] for p in before))  # B sees it as unverified

        org.verify("Inbox/Review.md", text="plant garlic", action="accept")
        self.wait(lambda: "garlic" in garden_b.read_text(), "the accepted decision on B")
        self.B.sync(force=True)
        after = self.on_b(self.B.recall, "plant garlic garden", 600)["passages"]
        hit = next(p for p in after if p["path"] == "Projects/Garden.md")
        self.assertTrue(hit["verified"])
        self.assertIn("[verified:: true]", garden_b.read_text())

    def test_machine_state_stays_out_of_the_synced_folder(self):
        gw = Gateway(self.B)  # make sure B has a gateway database too
        gw.close()
        for v in (self.pair.a, self.pair.b):
            names = {p.name for p in v.rglob("*")}
            self.assertFalse({"index.db", "gateway.db"} & names, v)
            self.assertFalse(any(n.endswith((".db-wal", ".db-shm")) for n in names), v)
        self.assertTrue((self.A.local_dir / "index.db").exists() or any(self.A.local_dir.glob("*.db")))
        self.assertNotEqual(self.A.local_dir, self.B.local_dir)
        with mock.patch.object(cloud, "provider_for", return_value=None):  # no cloud drive in a temp folder
            self.assertEqual([p["provider"] for p in cloud.sync_providers(self.pair.b)], ["Syncthing"])
            self.assertEqual([p["provider"] for p in self.on_b(self.B.health)["sync_providers"]], ["Syncthing"])


if __name__ == "__main__":
    unittest.main()
