r"""
backup_state.py - set up, back up and restore the local trading state.

WHAT IS STATE (all git-ignored; never committed)
  _restored/                 .env (secrets), db/, keys/, download/   - source of truth
  data/                      engine runtime state (screener, schedule, audit)
  src/quant/openalgo_platform/db/    the live OpenAlgo database (written while running)
  src/quant/openalgo_platform/keys/  OpenAlgo key material

USAGE
  python scripts/ops/backup_state.py --setup              create missing folders and README templates
                                                          (never creates a .env or any secret)
  python scripts/ops/backup_state.py                      back up now to E:\Algopro-restore
  python scripts/ops/backup_state.py --dest D:\\elsewhere   another backup folder
  python scripts/ops/backup_state.py --keep 30            dated backups to keep (default 30)
  python scripts/ops/backup_state.py --restore <folder>   copy a backup back (stop the stack first)

SAFETY
  * Backup only reads the source. SQLite files are copied with the SQLite backup API, so the
    copy is consistent even while OpenAlgo is running. The stack does not have to be stopped.
  * Restore refuses while OpenAlgo, the WebSocket proxy or the dashboard is listening, and
    first saves the current state to pre-restore-<stamp> so nothing is lost.
  * Retention deletes only folders named YYYYMMDD-HHMMSS in the backup root.
  * The log holds names, sizes and times only, never secret values.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import re
import shutil
import socket
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Iterable, List, Optional

ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_DEST = Path(r"E:\Algopro-restore")
DEFAULT_KEEP = 30
STAMP_RE = re.compile(r"^\d{8}-\d{6}$")
SERVICE_PORTS = (5000, 8050, 8765)  # OpenAlgo, dashboard, WebSocket proxy
TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"


def paths(root: Path) -> dict:
    tree = root / "src" / "quant" / "openalgo_platform"
    return {
        "restored": root / "_restored",
        "data": root / "data",
        "tree_db": tree / "db",
        "tree_keys": tree / "keys",
        "log": root / "Temp" / "run" / "backup.log",
    }


def _log(root: Path, message: str) -> None:
    p = paths(root)["log"]
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as fh:
        fh.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} {message}\n")


# ---------------------------------------------------------------- setup
def setup(root: Path) -> dict:
    """Create the folder layout and README templates. Never creates a .env or secret."""
    p = paths(root)
    created: List[str] = []
    for sub in ("db", "keys", "download"):
        d = p["restored"] / sub
        if not d.exists():
            d.mkdir(parents=True)
            created.append(str(d))
    if not p["data"].exists():
        p["data"].mkdir(parents=True)
        created.append(str(p["data"]))
    screened = p["data"] / "screened"
    if not screened.exists():
        screened.mkdir(parents=True)
        created.append(str(screened))
    # (template folder, target folder). data/screened/ gets its own templates (2026-11-27).
    for template_name, target in (("restored", p["restored"]), ("data", p["data"]),
                                  (("data", "screened"), screened)):
        src_dir = TEMPLATE_DIR.joinpath(*template_name) if isinstance(template_name, tuple) \
            else TEMPLATE_DIR / template_name
        for src in src_dir.glob("*"):
            dst = target / src.name
            if src.is_file() and not dst.exists():
                shutil.copy2(src, dst)
                created.append(str(dst))
    env_present = (p["restored"] / ".env").exists()
    _log(root, f"setup: created {len(created)} item(s); .env present={env_present}")
    return {"created": created, "env_present": env_present}


# ---------------------------------------------------------------- backup
def _copy_sqlite(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    src_conn = sqlite3.connect(f"file:{src.as_posix()}?mode=ro", uri=True)
    try:
        dst_conn = sqlite3.connect(dst)
        try:
            src_conn.backup(dst_conn)  # consistent copy, safe while the database is in use
        finally:
            dst_conn.close()
    finally:
        src_conn.close()


def _copy_tree_if_present(src: Path, dst: Path) -> List[str]:
    if not src.exists():
        return []
    if src.is_dir():
        shutil.copytree(src, dst, dirs_exist_ok=True)
        return [str(f.relative_to(dst)) for f in dst.rglob("*") if f.is_file()]
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    return [dst.name]


def prune(dest_root: Path, keep: int) -> List[str]:
    """Delete the oldest dated backup folders beyond `keep`. Only YYYYMMDD-HHMMSS names."""
    if not dest_root.exists():
        return []
    dated = sorted(p for p in dest_root.iterdir() if p.is_dir() and STAMP_RE.match(p.name))
    doomed = dated[:-keep] if keep > 0 and len(dated) > keep else []
    for p in doomed:
        shutil.rmtree(p)
    return [p.name for p in doomed]


def backup(root: Path, dest_root: Path, keep: int = DEFAULT_KEEP,
           now: Optional[datetime] = None) -> Path:
    p = paths(root)
    stamp = (now or datetime.now()).strftime("%Y%m%d-%H%M%S")
    target = dest_root / stamp
    n = 1
    while target.exists():  # two runs in the same second get a suffix, never overwrite
        target = dest_root / f"{stamp}-{n}"
        n += 1
    target.mkdir(parents=True)
    files: dict = {}
    files["_restored"] = _copy_tree_if_present(p["restored"], target / "_restored")
    files["data"] = _copy_tree_if_present(p["data"], target / "data")
    live_db = target / "openalgo_live_db"
    files["openalgo_live_db"] = []
    if p["tree_db"].exists():
        for f in sorted(p["tree_db"].iterdir()):
            if not f.is_file() or f.name.endswith(("-wal", "-shm", "-journal")):
                continue
            if f.suffix in (".db", ".sqlite", ".sqlite3"):
                _copy_sqlite(f, live_db / f.name)
            else:
                _copy_tree_if_present(f, live_db / f.name)
            files["openalgo_live_db"].append(f.name)
    files["openalgo_live_keys"] = _copy_tree_if_present(p["tree_keys"], target / "openalgo_live_keys")
    manifest = {
        "created": datetime.now().isoformat(timespec="seconds"),
        "root": str(root.name),
        "counts": {k: len(v) for k, v in files.items()},
        "files": files,
    }
    (target / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    removed = prune(dest_root, keep)
    _log(root, f"backup: {target} counts={manifest['counts']} pruned={len(removed)}")
    return target


# ---------------------------------------------------------------- restore
def _port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except OSError:
        return False


def restore(root: Path, src: Path, dest_root: Path, *, port_check=_port_open) -> Path:
    if not (src / "manifest.json").exists():
        raise SystemExit(f"not a backup folder (no manifest.json): {src}")
    busy = [port for port in SERVICE_PORTS if port_check(port)]
    if busy:
        raise SystemExit(f"stop the stack first; ports still listening: {busy}")
    safety = backup(root, dest_root, keep=0, now=datetime.now())  # keep the current state
    safety = safety.rename(safety.with_name("pre-restore-" + safety.name))
    p = paths(root)
    if (src / "_restored").exists():
        shutil.copytree(src / "_restored", p["restored"], dirs_exist_ok=True)
    if (src / "data").exists():
        shutil.copytree(src / "data", p["data"], dirs_exist_ok=True)
    if (src / "openalgo_live_db").exists():
        p["tree_db"].mkdir(parents=True, exist_ok=True)
        shutil.copytree(src / "openalgo_live_db", p["tree_db"], dirs_exist_ok=True)
    if (src / "openalgo_live_keys").exists():
        shutil.copytree(src / "openalgo_live_keys", p["tree_keys"], dirs_exist_ok=True)
    _log(root, f"restore: from {src}; current state saved to {safety}")
    return safety


# ---------------------------------------------------------------- CLI
def main(argv: Optional[Iterable[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Set up, back up and restore the local trading state.")
    ap.add_argument("--setup", action="store_true", help="create missing folders and templates")
    ap.add_argument("--restore", type=Path, metavar="FOLDER", help="restore from a backup folder")
    ap.add_argument("--dest", type=Path, default=DEFAULT_DEST, help="backup root (default E:\\Algopro-restore)")
    ap.add_argument("--keep", type=int, default=DEFAULT_KEEP, help="dated backups to keep (default 30)")
    args = ap.parse_args(list(argv) if argv is not None else None)

    if args.setup:
        report = setup(ROOT)
        print(f"setup: created {len(report['created'])} item(s)")
        if not report["env_present"]:
            print("MISSING: _restored\\.env - put your ORIGINAL .env there. It is never created here.")
        return 0
    if args.restore:
        safety = restore(ROOT, args.restore, args.dest)
        print(f"restored from {args.restore}. Current state saved to {safety}")
        return 0
    target = backup(ROOT, args.dest, keep=args.keep)
    print(f"backup written: {target}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
