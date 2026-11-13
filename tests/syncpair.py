"""Two real Syncthing peers on one machine, sharing one folder: the multi-device setup Cortex is
built for (one vault, a sync tool, an index per machine), with the real sync protocol over localhost.

    with SyncPair(tmp) as pair:
        pair.a / pair.b               # the two copies of the vault folder
        pair.wait(lambda: (pair.b / "Note.md").exists())
        pair.pause() / pair.resume()  # to make both sides edit at once (conflicts)

Needs the `syncthing` binary (Linux: apt-get install syncthing). No discovery, relays or NAT: the
two peers talk directly over 127.0.0.1 and never leave the machine.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import time
import urllib.request
from pathlib import Path

FOLDER = "cortex-vault"


def available() -> bool:
    return shutil.which("syncthing") is not None


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class Peer:
    def __init__(self, root: Path, name: str):
        self.name = name
        self.home = root / f"st-{name}"
        self.gui = _free_port()
        self.listen = _free_port()
        subprocess.run(["syncthing", "generate", f"--home={self.home}", "--no-default-folder"],
                       check=True, capture_output=True, timeout=60)
        self.key = re.search(r"<apikey>([^<]+)</apikey>", (self.home / "config.xml").read_text()).group(1)
        self.proc = subprocess.Popen(
            ["syncthing", "serve", f"--home={self.home}", "--no-browser", "--no-restart", "--no-upgrade",
             f"--gui-address=127.0.0.1:{self.gui}"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            env={**os.environ, "STNODEFAULTFOLDER": "1", "STNOUPGRADE": "1", "HOME": str(self.home)})
        deadline = time.time() + 30
        while True:
            try:
                self.id = self.api("GET", "/rest/system/status")["myID"]
                break
            except OSError:
                if time.time() > deadline:
                    raise RuntimeError(f"syncthing {name} did not start")
                time.sleep(0.2)
        self.api("PATCH", "/rest/config/options", {
            "listenAddresses": [f"tcp://127.0.0.1:{self.listen}"], "globalAnnounceEnabled": False,
            "localAnnounceEnabled": False, "relaysEnabled": False, "natEnabled": False, "urAccepted": -1,
            "crashReportingEnabled": False})

    def api(self, method: str, path: str, body=None):
        req = urllib.request.Request(f"http://127.0.0.1:{self.gui}{path}", method=method,
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={"X-API-Key": self.key, "Content-Type": "application/json"})
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # localhost: never via a proxy
        with opener.open(req, timeout=10) as r:
            data = r.read()
        return json.loads(data) if data.strip() else None

    def stop(self):
        try:
            self.api("POST", "/rest/system/shutdown")
            self.proc.wait(10)
        except Exception:
            self.proc.kill()


class SyncPair:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.a = self.root / "vault-a"
        self.b = self.root / "vault-b"

    def __enter__(self):
        self.a.mkdir(parents=True, exist_ok=True)
        self.b.mkdir(parents=True, exist_ok=True)
        self.pa, self.pb = Peer(self.root, "a"), Peer(self.root, "b")
        for me, other in ((self.pa, self.pb), (self.pb, self.pa)):
            me.api("POST", "/rest/config/devices", {"deviceID": other.id, "name": other.name,
                                                    "addresses": [f"tcp://127.0.0.1:{other.listen}"]})
        for me, path in ((self.pa, self.a), (self.pb, self.b)):
            me.api("POST", "/rest/config/folders", {
                "id": FOLDER, "label": "Cortex", "path": str(path), "type": "sendreceive",
                "devices": [{"deviceID": self.pa.id}, {"deviceID": self.pb.id}],
                "rescanIntervalS": 1, "fsWatcherEnabled": True, "fsWatcherDelayS": 1})
        self.wait(lambda: self._connected(), 60, "the two peers never connected")
        return self

    def __exit__(self, *exc):
        for p in (getattr(self, "pa", None), getattr(self, "pb", None)):
            if p:
                p.stop()

    def _connected(self) -> bool:
        try:
            return bool(self.pa.api("GET", "/rest/system/connections")["connections"].get(self.pb.id, {}).get("connected"))
        except OSError:
            return False

    def scan(self):
        for p in (self.pa, self.pb):
            try:
                p.api("POST", f"/rest/db/scan?folder={FOLDER}")
            except OSError:
                pass

    def wait(self, predicate, timeout: float = 60, what: str = "sync"):
        deadline = time.time() + timeout
        last_scan = 0.0
        while time.time() < deadline:
            if predicate():
                return
            if time.time() - last_scan > 2:
                self.scan()
                last_scan = time.time()
            time.sleep(0.3)
        raise TimeoutError(f"timed out waiting for {what}")

    def idle(self, timeout: float = 60):
        """Both peers report the folder fully synced (nothing left to pull or push)."""
        def done():
            try:
                return all(p.api("GET", f"/rest/db/status?folder={FOLDER}").get("needTotalItems", 1) == 0
                           and p.api("GET", f"/rest/db/completion?folder={FOLDER}").get("completion", 0) >= 100
                           for p in (self.pa, self.pb))
            except OSError:
                return False
        self.wait(done, timeout, "both peers to finish syncing")

    def pause(self):
        for p in (self.pa, self.pb):
            p.api("PATCH", f"/rest/config/folders/{FOLDER}", {"paused": True})

    def resume(self):
        for p in (self.pa, self.pb):
            p.api("PATCH", f"/rest/config/folders/{FOLDER}", {"paused": False})
