"""Backup and restore of ledger, audit log and limits, with a SHA-256 manifest.

Flow stage: Restart and recovery
Status: PHASE 3 IMPLEMENTED
Existing code to reuse or wrap (do not copy): none (new, flow item 18)

Rules:
- Backups go to a new timestamped folder. An existing folder is never overwritten.
- Restore checks every file's hash before copying.
- Restore refuses to overwrite an existing target unless force=True.
Backup folders should be on a different disk from the live files (owner setup).
"""
from __future__ import annotations

import hashlib
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def backup(files: Dict[str, object], dest_root, now: Optional[datetime] = None) -> Path:
    """Copy each named file into dest_root/<UTC stamp>/ and write manifest.json."""
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%SZ")
    dest = Path(dest_root) / stamp
    dest.mkdir(parents=True, exist_ok=False)
    manifest: Dict[str, Optional[str]] = {}
    for name, src in files.items():
        src = Path(src)
        if not src.exists():
            manifest[name] = None  # file not created yet (for example, no ledger on day one)
            continue
        shutil.copy2(src, dest / name)
        manifest[name] = _sha256(dest / name)
    (dest / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    return dest


def restore(backup_dir, targets: Dict[str, object], force: bool = False) -> Dict[str, Optional[str]]:
    """Copy files back to targets after checking each hash. Raises on any mismatch."""
    backup_dir = Path(backup_dir)
    manifest = json.loads((backup_dir / "manifest.json").read_text(encoding="utf-8"))
    for name, dst in targets.items():
        expected = manifest.get(name)
        if expected is None:
            continue
        if _sha256(backup_dir / name) != expected:
            raise ValueError(f"checksum mismatch for {name}; nothing restored for this file")
        dst = Path(dst)
        if dst.exists() and not force:
            raise FileExistsError(f"{dst} exists; pass force=True to overwrite")
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(backup_dir / name, dst)
    return manifest
