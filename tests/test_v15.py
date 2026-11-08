"""v1.5: import consent, packaged-exe connect configs, first-launch setup."""

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
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["CORTEX_SEMANTIC"] = "off"
os.environ.setdefault("CORTEX_LOCAL_DIR", tempfile.mkdtemp(prefix="cortex-test-local-"))

from cortex import cli  # noqa: E402
from cortex.core import Brain  # noqa: E402
from cortex.web import make_handler  # noqa: E402


class ConsentTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.t = Path(self.tmp.name)
        self.p = mock.patch.object(cli, "CONFIG", self.t / "config.json")
        self.p.start()
        self.b = Brain(self.t / "vault")
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.b))
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.b.close()
        self.p.stop()
        self.tmp.cleanup()

    def post(self, path, body):
        r = urllib.request.Request(f"http://127.0.0.1:{self.httpd.server_port}{path}", data=json.dumps(body).encode())
        r.add_header("X-Cortex", "1")
        r.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(r) as res:
                return res.status, json.loads(res.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_nothing_is_read_without_a_yes(self):
        self.assertFalse(cli.import_allowed())
        code, body = self.post("/api/import", {})
        self.assertEqual(code, 403)
        self.assertIn("Import my AI history", body["error"])
        self.assertEqual(self.post("/api/settings", {"import_history": True})[1], {"import_history": True})
        self.assertTrue(cli.import_allowed())
        self.assertEqual(self.post("/api/import", {})[0], 200)
        self.post("/api/settings", {"import_history": False})
        self.assertFalse(cli.import_allowed())

    def test_mcp_server_does_not_import_without_consent(self):
        from cortex import mcp_server
        started = []
        with mock.patch("cortex.importers.Importer.run", side_effect=lambda *a, **k: started.append(1)), \
                mock.patch.object(mcp_server.Server, "serve", lambda self: None):
            mcp_server.run(str(self.t / "vault2"))
            self.assertEqual(started, [])


class ExeTest(unittest.TestCase):
    def test_connect_config_points_at_the_exe(self):
        with mock.patch.object(cli, "FROZEN", True), mock.patch.object(cli.sys, "executable", r"C:\Apps\Cortex\Cortex.exe"):
            e = cli._server_entry(Path("C:/Users/me/Documents/Cortex"), "cursor")
        self.assertEqual(e["command"], r"C:\Apps\Cortex\Cortex.exe")
        self.assertEqual(e["args"][:2], ["mcp", "--vault"])
        e2 = cli._server_entry(Path("/v"), "cursor")
        self.assertTrue(e2["args"][0].endswith("run.py"))

    def test_first_launch_creates_the_brain(self):
        with tempfile.TemporaryDirectory() as t:
            home = Path(t)
            with mock.patch.object(cli, "CONFIG", home / "config.json"), mock.patch.object(cli, "DEFAULT_VAULT", home / "Documents" / "Cortex"), \
                    mock.patch("cortex.web.run") as run_app, mock.patch("builtins.print"), mock.patch.dict(os.environ, {"CORTEX_VAULT": ""}):
                cli.main(["app", "--no-browser"])
            self.assertTrue((home / "Documents" / "Cortex" / "_CORTEX.md").exists())
            self.assertEqual(json.loads((home / "config.json").read_text())["vault"], str((home / "Documents" / "Cortex").resolve()))
            run_app.assert_called_once()


if __name__ == "__main__":
    unittest.main()


class RobustnessTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.b = Brain(Path(self.tmp.name) / "v")

    def tearDown(self):
        self.b.close()
        self.tmp.cleanup()

    def test_retries_when_windows_briefly_locks_a_file(self):
        real = os.replace
        calls = []

        def flaky(a, b):
            calls.append(1)
            if len(calls) < 3:
                raise PermissionError(32, "being used by another process")
            return real(a, b)
        with mock.patch("cortex.core.os.replace", side_effect=flaky), mock.patch("cortex.core.time.sleep"):
            self.b.write("A.md", "# A\nsaved")
        self.assertEqual((Path(self.tmp.name) / "v" / "A.md").read_text(), "# A\nsaved\n")
        self.assertFalse(list((Path(self.tmp.name) / "v").glob(".*.tmp")))

    def test_permanent_lock_is_reported_clearly(self):
        with mock.patch("cortex.core.os.replace", side_effect=PermissionError(32, "in use")), mock.patch("cortex.core.time.sleep"):
            with self.assertRaises(PermissionError) as e:
                self.b.write("A.md", "x")
        self.assertIn("locked by another program", str(e.exception))
        self.assertFalse(list((Path(self.tmp.name) / "v").glob(".*.tmp")))  # no temp litter

    def test_concurrent_saves_of_one_note_never_corrupt_it(self):
        bodies = [f"# Note\n{'v%d ' % i * 2000}" for i in range(12)]
        errs = []

        def save(t):
            try:
                self.b.write("Shared.md", t, agent="you")
            except Exception as e:  # noqa: BLE001
                errs.append(e)
        threads = [threading.Thread(target=save, args=(t,)) for t in bodies]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual(errs, [])
        final = (Path(self.tmp.name) / "v" / "Shared.md").read_text()
        self.assertIn(final.rstrip("\n"), [t for t in bodies])  # exactly one complete version, never a mix


class PortTest(unittest.TestCase):
    def test_second_launch_and_busy_port(self):
        import socket
        from cortex import web
        with tempfile.TemporaryDirectory() as t:
            b = Brain(Path(t) / "v")
            first, port = web._bind(b, 47000 + os.getpid() % 1000)
            threading.Thread(target=first.serve_forever, daemon=True).start()
            again, port2 = web._bind(b, port)
            self.assertIsNone(again)  # Cortex already there: reuse it
            self.assertEqual(port2, port)
            blocker = socket.socket()
            blocker.bind(("127.0.0.1", port + 5))
            blocker.listen()
            other, port3 = web._bind(b, port + 5)  # someone else's program: move on
            self.assertEqual(port3, port + 6)
            other.server_close()
            blocker.close()
            first.shutdown()
            first.server_close()
            b.close()
