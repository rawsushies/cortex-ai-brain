"""Local + cloud: index stays local, two devices share one synced folder, conflicts, backups, the move."""

import json
import os
import sys
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["CORTEX_SEMANTIC"] = "off"
os.environ.setdefault("CORTEX_LOCAL_DIR", tempfile.mkdtemp(prefix="cortex-test-local-"))

import cortex.core as C  # noqa: E402
from cortex import cloud  # noqa: E402
from cortex.core import Brain  # noqa: E402


class Tmp(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.t = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()


class LocalIndexTest(Tmp):
    def test_index_never_inside_the_synced_vault(self):
        v = self.t / "My Drive" / "Cortex"
        (v / ".cortex").mkdir(parents=True)
        (v / ".cortex" / "index.db").write_text("old index from v1.3")  # would have been synced before
        (v / "N.md").write_text("# N\nhello")
        b = Brain(v)
        b.sync(force=True)
        self.assertEqual(b.search("hello")[0].path, "N.md")
        self.assertFalse((v / ".cortex" / "index.db").exists())
        self.assertFalse(any(p.name.startswith("index.db") for p in v.rglob("*")))
        self.assertNotIn(str(v), str(b.local_dir))
        b.close()


class TwoDevicesTest(Tmp):
    """Two PCs open the same Google Drive folder: each has its own index and logbook file."""

    def open_as(self, device, vault, local):
        with mock.patch.dict(os.environ, {"CORTEX_LOCAL_DIR": str(self.t / local)}), mock.patch.object(C, "DEVICE", device):
            return Brain(vault)

    def test_shared_brain(self):
        v = self.t / "drive" / "Cortex"
        laptop = self.open_as("LAPTOP", v, "local-laptop")
        desktop = self.open_as("DESKTOP", v, "local-desktop")
        self.assertNotEqual(laptop.local_dir, desktop.local_dir)
        with mock.patch.object(C, "DEVICE", "LAPTOP"):
            laptop.write("Projects/Trip.md", "# Trip\nBook flights to Penang", agent="claude")
        with mock.patch.object(C, "DEVICE", "DESKTOP"):
            desktop.sync(force=True)  # "Drive synced the file over"
            self.assertEqual(desktop.recall("penang flights", 300)["passages"][0]["path"], "Projects/Trip.md")
            desktop.write("Projects/Trip.md", "\n- Hotel booked", agent="codex", mode="append")
        files = sorted(p.name for p in (v / ".cortex" / "activity").glob("*.jsonl"))
        self.assertEqual(files, ["DESKTOP.jsonl", "LAPTOP.jsonl"])  # never the same file
        acts = laptop.activity(10)
        self.assertEqual([(a["device"], a["agent"]) for a in acts], [("DESKTOP", "codex"), ("LAPTOP", "claude")])
        with mock.patch.object(C, "DEVICE", "LAPTOP"):
            devs = [d["device"] for d in cloud.devices(laptop)]
        self.assertEqual(sorted(devs), ["DESKTOP", "LAPTOP"])
        laptop.close()
        desktop.close()

    def test_time_zones_and_half_synced_lines(self):
        v = self.t / "v"
        b = self.open_as("A", v, "la")
        d = v / ".cortex" / "activity"
        d.mkdir(parents=True)
        # Tokyo 09:00 is earlier than London 01:00 the same day (00:00 UTC vs 01:00 UTC)
        (d / "TOKYO.jsonl").write_text(json.dumps({"ts": "2026-11-08T09:00:00+09:00", "agent": "a", "action": "x"}) + "\n")
        (d / "LONDON.jsonl").write_text(json.dumps({"ts": "2026-11-08T01:00:00+00:00", "agent": "b", "action": "y"}) + "\n{\"ts\": \"2026-11-08T0")
        self.assertEqual([a["agent"] for a in b.activity(5)], ["b", "a"])
        b.close()

    def test_legacy_single_logbook_still_read(self):
        v = self.t / "v"
        (v / ".cortex").mkdir(parents=True)
        (v / ".cortex" / "activity.jsonl").write_text(json.dumps({"ts": "2026-01-01T00:00:00+00:00", "agent": "old", "action": "x"}) + "\n")
        b = self.open_as("A", v, "la")
        b.log("new", "y")
        self.assertEqual([a["agent"] for a in b.activity(5)], ["new", "old"])
        b.close()


class ConflictTest(Tmp):
    def test_conflicts_in_health(self):
        v = self.t / "v"
        b = Brain(v)
        for name in ("Plan.md", "Plan (1).md", "Chapter (1).md", "Conflict resolution guide.md", "Notes.md", "Notes [Conflict].md"):
            (v / name).write_text(f"# {name}\nsome words in this note here")
        h = b.health()
        self.assertEqual(sorted(c["note"] for c in h["sync_conflicts"]), ["Notes [Conflict].md", "Plan (1).md"])
        self.assertFalse(any("Plan (1).md" in d for d in h["duplicates"]))  # reported once, as a conflict
        b.close()

    def test_syncthing_conflict_copies(self):
        v = self.t / "v"
        b = Brain(v)
        (v / "Projects").mkdir(parents=True, exist_ok=True)
        for name in ("Projects/Tower.md", "Projects/Tower.sync-conflict-20261112-091500-ABCDEFG.md", "Ideas.sync-conflict-20261112-091500.md", "sync-conflict notes.md"):
            (v / name).write_text(f"# {name}\nsome words in this note here")
        c = {x["note"]: x["original"] for x in b.health()["sync_conflicts"]}
        self.assertEqual(c, {"Projects/Tower.sync-conflict-20261112-091500-ABCDEFG.md": "Projects/Tower.md", "Ideas.sync-conflict-20261112-091500.md": None})
        b.close()


class BackupTest(Tmp):
    def make_vault(self):
        v = self.t / "vault"
        b = Brain(v)
        b.write("A.md", "# A\nalpha", agent="you")
        b.write("A.md", "# A\nalpha v2", agent="you")
        b.write("Projects/B.md", "# B\nbeta", agent="claude")
        (v / ".trash").mkdir(exist_ok=True)
        (v / ".trash" / "old.md").write_text("trash")
        return v, b

    def test_roundtrip(self):
        v, b = self.make_vault()
        r = cloud.backup(v, self.t / "backups")
        names = zipfile.ZipFile(r["path"]).namelist()
        self.assertIn("A.md", names)
        self.assertIn("Projects/B.md", names)
        self.assertFalse(any(n.startswith((".trash", ".cortex/history")) or "index.db" in n for n in names))
        self.assertTrue(any(n.startswith(".cortex/activity/") for n in names))
        self.assertFalse(list((self.t / "backups").glob("*.partial")))
        out = cloud.restore(Path(r["path"]), self.t / "restored")
        self.assertEqual((self.t / "restored" / "A.md").read_text(), (v / "A.md").read_text())
        self.assertEqual(out["files"], r["files"])
        with self.assertRaises(FileExistsError):
            cloud.restore(Path(r["path"]), self.t / "restored")  # never over an existing folder
        full = cloud.backup(v, self.t / "backups2", include_history=True)
        self.assertTrue(any(n.startswith(".cortex/history/") for n in zipfile.ZipFile(full["path"]).namelist()))
        b.close()

    def test_refuses_backup_inside_vault(self):
        v, b = self.make_vault()
        with self.assertRaises(ValueError):
            cloud.backup(v, v / "backups")
        b.close()

    def test_damaged_and_malicious_backups(self):
        v, b = self.make_vault()
        r = cloud.backup(v, self.t / "bk")
        bad = self.t / "damaged.zip"
        with zipfile.ZipFile(r["path"]) as src, zipfile.ZipFile(bad, "w") as dst:
            for n in src.namelist():
                dst.writestr(n, b"tampered" if n == "A.md" else src.read(n))
        with self.assertRaises(IOError):
            cloud.restore(bad, self.t / "r1")
        evil = self.t / "evil.zip"
        with zipfile.ZipFile(evil, "w") as z:
            z.writestr("../../escaped.md", "x")
            z.writestr("cortex-manifest.json", json.dumps({"files": {"../../escaped.md": "0" * 64}}))
        with self.assertRaises(ValueError):
            cloud.restore(evil, self.t / "r2" / "deep")
        self.assertFalse((self.t / "escaped.md").exists())
        b.close()

    def test_prune_keeps_recent_plus_monthly(self):
        d = self.t / "bk"
        d.mkdir()
        for stamp in ["20260101-100000", "20260115-100000", "20260201-100000", "20260210-100000",
                      "20261101-100000", "20261102-100000", "20261103-100000"]:
            (d / f"{cloud.BACKUP_PREFIX}{stamp}.zip").write_bytes(b"x")
        cloud.prune_backups(d, keep=3)
        left = sorted(f.name[len(cloud.BACKUP_PREFIX):-4] for f in d.glob("*.zip"))
        self.assertEqual(left, ["20260115-100000", "20260210-100000", "20261101-100000", "20261102-100000", "20261103-100000"])

    def test_backup_due(self):
        d = self.t / "bk"
        self.assertFalse(cloud.backup_due({}))
        self.assertTrue(cloud.backup_due({"backup_dir": str(d)}))
        d.mkdir()
        (d / f"{cloud.BACKUP_PREFIX}{time.strftime('%Y%m%d-%H%M%S')}.zip").write_bytes(b"x")
        self.assertFalse(cloud.backup_due({"backup_dir": str(d)}))
        self.assertFalse(cloud.backup_due({"backup_dir": str(d), "auto_backup": False}))


class MoveTest(Tmp):
    def test_move_copies_and_verifies(self):
        v = self.t / "vault"
        b = Brain(v)
        b.write("A.md", "# A\n" + "x" * 5000, agent="you")
        b.close()
        r = cloud.move_vault(v, self.t / "My Drive" / "Cortex")
        self.assertEqual((self.t / "My Drive/Cortex/A.md").read_text(), (v / "A.md").read_text())
        self.assertTrue((v / "A.md").exists())  # original untouched
        self.assertGreaterEqual(r["files"], 2)
        with self.assertRaises(FileExistsError):
            cloud.move_vault(v, self.t / "My Drive" / "Cortex")
        with self.assertRaises(ValueError):
            cloud.move_vault(v, v / "inside")

    def test_detection(self):
        # Use this OS's real Google Drive location (faking another OS breaks pathlib on Windows).
        home = self.t / "home"
        gd = home / ("My Drive" if os.name == "nt" else "GoogleDrive")
        if sys.platform == "darwin":
            gd = home / "Library" / "CloudStorage" / "GoogleDrive-me@example.com" / "My Drive"
        gd.mkdir(parents=True)
        (home / "Dropbox").mkdir()
        with mock.patch.object(cloud, "HOME", home):
            found = cloud.cloud_folders()
            self.assertEqual(found[0]["provider"], "Google Drive")
            self.assertIn("Dropbox", [f["provider"] for f in found])
            self.assertEqual(cloud.provider_for(gd / "Cortex")["provider"], "Google Drive")
            self.assertIsNone(cloud.provider_for(home / "Documents" / "Cortex"))
        self.assertEqual(cloud.provider_for(Path("G:/My Drive/Cortex"))["provider"], "Google Drive")

    def test_cli_cloud_end_to_end(self):
        home = self.t / "home"
        drive = home / ("My Drive" if os.name == "nt" else "GoogleDrive")
        if sys.platform == "darwin":
            drive = home / "Library" / "CloudStorage" / "GoogleDrive-me@example.com" / "My Drive"
        drive.mkdir(parents=True)
        v = home / "Documents" / "Cortex"
        from cortex import cli
        with mock.patch.object(cli, "CONFIG", home / ".config/cortex/config.json"), mock.patch.object(cli, "HOME", home), \
                mock.patch.object(cloud, "HOME", home), mock.patch.object(cli.shutil, "which", return_value=None), \
                mock.patch.dict(os.environ, {"APPDATA": str(home / "AppData"), "OneDrive": ""}), \
                mock.patch("builtins.print"):
            cli.main(["init", str(v)])
            b = Brain(v)
            b.write("Projects/X.md", "# X\nmy project", agent="you")
            b.close()
            cli.main(["cloud", "--yes"])
            cfg = json.loads((home / ".config/cortex/config.json").read_text())
        self.assertEqual(cfg["vault"], str((drive / "Cortex").resolve()))
        self.assertTrue((drive / "Cortex" / "Projects" / "X.md").exists())
        self.assertTrue(list((drive / "Cortex Backups").glob("Cortex-backup-*.zip")))
        self.assertTrue((v / "Projects/X.md").exists())  # old copy kept as a fallback


if __name__ == "__main__":
    unittest.main()
