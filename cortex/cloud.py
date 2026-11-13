"""Local + cloud: keep the brain on your PC AND in Google Drive (or OneDrive/Dropbox/iCloud).

How it works
- Your notes are plain files, so the sync app on your PC (Google Drive for desktop)
  uploads them and keeps every device in step. Cortex works on the local copy at full
  speed, offline too; Drive syncs in the background.
- What must NOT be synced stays on each PC: the SQLite search index and import state
  (see core.local_state_dir). A database inside a synced folder gets copied mid-write
  and corrupted, and two PCs would fight over it. It rebuilds from the notes anyway.
- The shared logbook is one file per device, so two PCs never edit the same file.
- Backups: dated, verified zip snapshots in Drive. Sync mirrors mistakes (a deleted note
  is deleted everywhere); a snapshot lets you go back to any day.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import string
import sys
import time
import zipfile
from datetime import datetime
from pathlib import Path

HOME = Path.home()
BACKUP_PREFIX = "Cortex-backup-"
EXCLUDE_DIRS = {".trash", "node_modules", ".git"}
# Only the bracketed forms sync apps generate ("Plan [Conflict].md", "Plan (Ana's conflicted copy).md",
# "Plan (conflict 2026-11-08).md"), never a note that merely has "conflict" in its title.
CONFLICT_PATTERNS = (
    re.compile(r"[\(\[][^()\[\]]*\bconflict(?:ed copy)?\b[^()\[\]]*[\)\]]", re.I),
)

SYNCTHING_CONFLICT = re.compile(r"^(.*)\.sync-conflict-\d{8}-\d{6}(?:-[A-Z0-9]{7})?\.md$", re.I)

# ── finding Google Drive (and friends) ──────────────────────────────────
def cloud_folders() -> list[dict]:
    """Cloud-synced folders on this computer, Google Drive first."""
    found: list[dict] = []

    def add(provider: str, p: Path):
        try:
            if p.is_dir() and not any(f["path"] == str(p) for f in found):
                found.append({"provider": provider, "path": str(p)})
        except OSError:
            pass

    if os.name == "nt":
        # Drive for desktop: "Stream files" mounts a drive letter (G: by default);
        # "Mirror files" keeps a real folder, by default %USERPROFILE%\My Drive.
        for letter in string.ascii_uppercase[3:]:
            add("Google Drive", Path(f"{letter}:/My Drive"))
        add("Google Drive", HOME / "My Drive")
        add("Google Drive", HOME / "Google Drive")
        if os.environ.get("OneDrive"):
            add("OneDrive", Path(os.environ["OneDrive"]))
    elif sys.platform == "darwin":
        cs = HOME / "Library" / "CloudStorage"
        if cs.is_dir():
            for d in sorted(cs.glob("GoogleDrive-*")):
                add("Google Drive", d / "My Drive")
            for d in sorted(cs.glob("OneDrive*")):
                add("OneDrive", d)
        add("Google Drive", HOME / "Google Drive" / "My Drive")
        add("Google Drive", HOME / "Google Drive")
        add("iCloud Drive", HOME / "Library" / "Mobile Documents" / "com~apple~CloudDocs")
    else:
        add("Google Drive", HOME / "GoogleDrive")
        add("Google Drive", HOME / "google-drive")
    add("Dropbox", HOME / "Dropbox")
    return found


def provider_for(vault: Path) -> dict | None:
    """Which cloud folder (if any) the vault lives in."""
    v = str(Path(vault).resolve()).lower()
    for f in cloud_folders():
        root = str(Path(f["path"]).resolve()).lower()
        if v == root or v.startswith(root.rstrip("\\/") + os.sep.lower()) or v.startswith(root.rstrip("\\/") + "/"):
            return f
    for name, marker in (("Google Drive", "my drive"), ("Google Drive", "googledrive"), ("OneDrive", "onedrive"),
                         ("Dropbox", "dropbox"), ("iCloud Drive", "clouddocs")):
        if marker in v:
            return {"provider": name, "path": None}
    return None


# ── file listing & hashing ───────────────────────────────────────────────
def _files(root: Path, include_history: bool = True):
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = Path(dirpath).relative_to(root)
        dirnames[:] = [d for d in dirnames if d not in EXCLUDE_DIRS and not os.path.islink(os.path.join(dirpath, d))
                       and not (not include_history and rel_dir.as_posix() == ".cortex" and d == "history")]
        for f in filenames:
            full = Path(dirpath) / f
            if full.is_symlink() or f.endswith((".tmp", ".partial")) or f.startswith(("index.db", "~$")):
                continue
            yield full


def _sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ── moving the vault into the cloud ──────────────────────────────────────
def move_vault(src: Path, dest: Path) -> dict:
    """Copy the vault to `dest`, verify every file by checksum, and return a report.
    The original is left untouched; the caller switches the config only if this succeeds."""
    src, dest = Path(src).resolve(), Path(dest).resolve()
    if dest == src or src in dest.parents:
        raise ValueError("the destination is inside the current vault")
    if dest.exists() and any(dest.iterdir()):
        raise FileExistsError(f"{dest} already exists and isn't empty; choose a new folder")
    files = list(_files(src))
    dest.mkdir(parents=True, exist_ok=True)
    for f in files:
        target = dest / f.relative_to(src)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(f, target)
    bad = [str(f.relative_to(src)) for f in files if _sha256(f) != _sha256(dest / f.relative_to(src))]
    if bad:
        raise IOError(f"{len(bad)} file(s) didn't copy correctly (e.g. {bad[0]}); the original vault is unchanged")
    return {"from": str(src), "to": str(dest), "files": len(files), "bytes": sum(f.stat().st_size for f in files)}


# ── backups ──────────────────────────────────────────────────────────────
def backup(vault: Path, dest_dir: Path, keep: int = 30, include_history: bool = False) -> dict:
    """Zip the vault into dest_dir/Cortex-backup-YYYYMMDD-HHMMSS.zip with a checksum manifest.
    Written to a temporary name and renamed at the end, so a sync app never uploads half a backup."""
    vault, dest_dir = Path(vault).resolve(), Path(dest_dir).expanduser().resolve()
    if dest_dir == vault or vault in dest_dir.parents:
        raise ValueError("put backups outside the vault (otherwise each backup would contain the last one)")
    dest_dir.mkdir(parents=True, exist_ok=True)
    name = f"{BACKUP_PREFIX}{datetime.now().strftime('%Y%m%d-%H%M%S')}.zip"
    tmp = dest_dir / (name + ".partial")
    manifest = {"created": datetime.now().astimezone().isoformat(timespec="seconds"), "vault": vault.name, "files": {}}
    total = 0
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for f in _files(vault, include_history):
            rel = f.relative_to(vault).as_posix()
            z.write(f, rel)
            manifest["files"][rel] = _sha256(f)
            total += f.stat().st_size
        z.writestr("cortex-manifest.json", json.dumps(manifest, indent=1))
    final = dest_dir / name
    os.replace(tmp, final)
    pruned = prune_backups(dest_dir, keep)
    return {"path": str(final), "files": len(manifest["files"]), "bytes": total, "zip_bytes": final.stat().st_size, "pruned": pruned}


def list_backups(dest_dir: Path) -> list[dict]:
    d = Path(dest_dir).expanduser()
    if not d.is_dir():
        return []
    out = []
    for f in sorted(d.glob(f"{BACKUP_PREFIX}*.zip"), reverse=True):
        m = re.search(r"(\d{8}-\d{6})", f.name)
        when = datetime.strptime(m.group(1), "%Y%m%d-%H%M%S").isoformat() if m else ""
        out.append({"name": f.name, "path": str(f), "created": when, "bytes": f.stat().st_size})
    return out


def prune_backups(dest_dir: Path, keep: int) -> int:
    """Keep the newest `keep`, plus one per month for older ones (cheap long-term history)."""
    items = list_backups(dest_dir)
    keep_set = {b["path"] for b in items[:max(1, keep)]}
    months = set()
    for b in items[keep:]:
        month = b["created"][:7]
        if month not in months:
            months.add(month)
            keep_set.add(b["path"])
    removed = 0
    for b in items:
        if b["path"] not in keep_set:
            try:
                Path(b["path"]).unlink()
                removed += 1
            except OSError:
                pass
    return removed


def restore(zip_path: Path, target: Path) -> dict:
    """Restore a backup into a NEW folder (never over your current vault), verifying checksums."""
    zip_path, target = Path(zip_path).expanduser().resolve(), Path(target).expanduser().resolve()
    if target.exists() and any(target.iterdir()):
        raise FileExistsError(f"{target} isn't empty; restore into a new folder")
    with zipfile.ZipFile(zip_path) as z:
        try:
            manifest = json.loads(z.read("cortex-manifest.json"))
        except KeyError:
            raise ValueError("not a Cortex backup (no manifest)")
        target.mkdir(parents=True, exist_ok=True)
        for rel, digest in manifest["files"].items():
            out = (target / rel).resolve()
            if target not in out.parents:  # "zip slip": an entry like ../../x must not escape
                raise ValueError(f"unsafe path in backup: {rel}")
            out.parent.mkdir(parents=True, exist_ok=True)
            data = z.read(rel)
            if hashlib.sha256(data).hexdigest() != digest:
                raise IOError(f"{rel} is damaged in this backup")
            out.write_bytes(data)
    return {"restored_to": str(target), "files": len(manifest["files"]), "created": manifest.get("created")}


# ── sync conflicts ───────────────────────────────────────────────────────
def sync_providers(vault: Path) -> list[dict]:
    """Which sync tools carry this vault. One is fine; two (say Syncthing inside a Google Drive folder)
    copy each other's half-written files and multiply conflicts."""
    vault = Path(vault).resolve()
    found = []
    for d in [vault, *vault.parents]:
        if (d / ".stfolder").exists():
            found.append({"provider": "Syncthing", "path": str(d)})
            break
    cloud = provider_for(vault)
    if cloud:
        found.append({"provider": cloud["provider"], "path": cloud.get("path") or str(vault)})
    return found


def conflict_copies(paths: list[str]) -> list[dict]:
    """Notes that look like a sync app's duplicate when two devices edited at once.
    "Plan (1).md" only counts when "Plan.md" also exists, to avoid false alarms."""
    existing = {p.lower() for p in paths}
    out = []
    for p in paths:
        stem = p[:-3]
        m = re.match(r"^(.*?) \(\d+\)$", stem)
        if m and (m.group(1) + ".md").lower() in existing:
            out.append({"note": p, "original": m.group(1) + ".md"})
            continue
        st = SYNCTHING_CONFLICT.match(p)  # Syncthing: "Plan.sync-conflict-20261112-091500-ABCDEFG.md"
        if st:
            base = st.group(1) + ".md"
            out.append({"note": p, "original": base if base.lower() in existing else None})
            continue
        if any(pat.search(Path(p).name) for pat in CONFLICT_PATTERNS):
            base = re.sub(r"\s*[\(\[][^()\[\]]*conflict[^()\[\]]*[\)\]]", "", stem, flags=re.I).strip() + ".md"
            out.append({"note": p, "original": base if base.lower() in existing else None})
    return out


def devices(brain) -> list[dict]:
    """Devices that have written to this brain (one logbook file each), newest first."""
    out = []
    if brain.activity_dir.is_dir():
        for f in brain.activity_dir.glob("*.jsonl"):
            last = brain._tail(f, 1)
            out.append({"device": f.stem, "last_seen": last[-1]["ts"] if last else None, "this": f.stem == _device()})
    if not any(d["this"] for d in out):
        out.append({"device": _device(), "last_seen": None, "this": True})
    from .core import _ts_key
    return sorted(out, key=lambda d: (d["this"], _ts_key(d["last_seen"] or "")), reverse=True)


def _device() -> str:
    from .core import DEVICE
    return DEVICE


def status(brain, cfg: dict) -> dict:
    prov = provider_for(brain.root)
    bdir = cfg.get("backup_dir")
    backups = list_backups(Path(bdir)) if bdir else []
    return {
        "vault": str(brain.root),
        "cloud": prov,
        "detected": cloud_folders(),
        "device": _device(),
        "devices": devices(brain),
        "local_index": str(brain.local_dir),
        "backup_dir": bdir,
        "backups": backups[:10],
        "backup_count": len(backups),
        "last_backup": backups[0]["created"] if backups else None,
        "auto_backup": bool(cfg.get("auto_backup", bool(bdir))),
    }


def backup_due(cfg: dict, every_hours: float = 24) -> bool:
    bdir = cfg.get("backup_dir")
    if not bdir or not cfg.get("auto_backup", True):
        return False
    items = list_backups(Path(bdir))
    if not items:
        return True
    try:
        last = datetime.fromisoformat(items[0]["created"]).timestamp()
    except ValueError:
        return True
    return time.time() - last > every_hours * 3600
