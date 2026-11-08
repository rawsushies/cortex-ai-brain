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
