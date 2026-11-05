"""
upgrade_check.py - Pin and verify the OpenAlgo folder's upstream identity.

WHY
---
The openalgo/ folder is intentionally a *pristine* copy of the upstream
OpenAlgo repository. That is the property that makes upgrades safe: when a
new OpenAlgo version is released, we wipe the folder and re-clone, with no
merge conflicts and no hand-edits to undo.

The risk is that a partial upgrade (some files updated, others not) would
silently produce a Frankenstein install that boots but misbehaves - the worst
kind of failure. To detect that, we record a *fingerprint* of the folder
when we know it is good, and refuse to start if the fingerprint changes
without an explicit pin update.

FINGERPRINT
----------
We do NOT rely on a git SHA because openalgo/ is not a git checkout (no
.git/ inside it). Instead we hash the four files that change on every
upstream release: install/, requirements.txt, pyproject.toml, and the
top-level .gitignore. Those four together are enough to detect a version
change while being cheap to compute (no full-tree SHA).

USAGE
-----
    # Show the current fingerprint
    python scripts/upgrade_check.py

    # Show what changed since the last pin
    python scripts/upgrade_check.py --diff

    # Update the pin to the current folder's fingerprint (after upgrading)
    python scripts/upgrade_check.py --update-pin

    # Check a folder other than ./openalgo
    python scripts/upgrade_check.py --openalgo-dir <path>
"""
from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_OA = ROOT / "openalgo"
PIN_PATH = ROOT / "openalgo.UPSTREAM_PIN"

# Files that change on every upstream release. These are the *only* files we
# fingerprint - everything else (.env, db/, keys/, download/) is local state
# that we never care about for upgrade detection.
WATCHED = (
    "requirements.txt",
    "pyproject.toml",
    ".gitignore",
)
WATCHED_DIRS = ("install",)


def _hash_file(path: Path) -> str:
    """SHA-256 of a single file, hex digest."""
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _hash_dir(path: Path) -> str:
    """SHA-256 of the concatenation of every file under path, sorted by rel path.

    We hash the rel-path + newline + content so two dirs with the same files
    in a different order still differ. That matters when upstream shuffles
    install/ files.
    """
    h = hashlib.sha256()
    if not path.is_dir():
        return "MISSING"
    for p in sorted(path.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(path).as_posix()
        h.update(rel.encode("utf-8"))
        h.update(b"\0")
        with p.open("rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        h.update(b"\0")
    return h.hexdigest()


def fingerprint(oa: Path) -> dict:
    """Compute a fingerprint of the watched files in oa/."""
    if not oa.is_dir():
        raise FileNotFoundError(f"openalgo dir not found: {oa}")
    # The folder path is reported but NOT hashed: a path-dependent pin can never match on another machine.
    fp: dict = {"openalgo_dir": str(oa.resolve())}
    for name in WATCHED:
        p = oa / name
        fp[name] = _hash_file(p) if p.is_file() else "MISSING"
    for d in WATCHED_DIRS:
        fp[d + "/"] = _hash_dir(oa / d)
    # Roll up to one short hash for the pin file (12 hex chars is plenty
    # for collision detection here - 2^48 is enormous vs the ~once-a-year
    # rate of upstream releases we expect).
    content = sorted((k, v) for k, v in fp.items() if k != "openalgo_dir")
    rolled = hashlib.sha256(repr(content).encode("utf-8")).hexdigest()[:12]
    fp["_rolled"] = rolled
    return fp


def read_pin() -> str | None:
    if not PIN_PATH.is_file():
        return None
    for line in PIN_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        return line
    return None


def write_pin(value: str, note: str = "") -> None:
    body = f"# OpenAlgo upstream fingerprint pin\n#\n"
    if note:
        body += f"# {note}\n"
    body += f"# Regenerate with: python scripts/upgrade_check.py --update-pin\n"
    body += f"{value}\n"
    PIN_PATH.write_text(body, encoding="utf-8")
    print(f"[upgrade_check] pin updated: {value}")


def main() -> int:
    p = argparse.ArgumentParser(description="Verify OpenAlgo folder identity.")
    p.add_argument("--openalgo-dir", type=Path, default=DEFAULT_OA,
                   help=f"Path to openalgo/ (default: {DEFAULT_OA})")
    p.add_argument("--diff", action="store_true",
                   help="Show what changed since the last pin")
    p.add_argument("--update-pin", action="store_true",
                   help="Overwrite the pin with the current folder's fingerprint")
    p.add_argument("--strict", action="store_true",
                   help="Exit non-zero if the pin does not match (used by start_openalgo.py)")
    p.add_argument("--allow-unpinned", action="store_true",
                   help="Do not fail when the pin file is missing (used by start_openalgo.py for first run)")
    args = p.parse_args()

    try:
        current = fingerprint(args.openalgo_dir)
    except FileNotFoundError as e:
        print(f"[FAIL] {e}")
        return 1

    pinned = read_pin()

    if args.update_pin:
        note = ""
        if pinned:
            note = f"Previous pin: {pinned}"
        write_pin(current["_rolled"], note=note)
        return 0

    print(f"[upgrade_check] openalgo dir: {current['openalgo_dir']}")
    print(f"[upgrade_check] current fingerprint: {current['_rolled']}")

    # Handle the no-pin case (first run / pin never written).
    if pinned is None or pinned == "NOT_PINNED_YET":
        print(f"[upgrade_check] no real pin in {PIN_PATH.name}")
        if args.strict and not args.allow_unpinned:
            print("             refusing to proceed; run --update-pin first.")
            return 1
        print("             (run --update-pin to record the current version)")
        return 0

    print(f"[upgrade_check] pinned:               {pinned}")
    if args.diff:
        if pinned == current["_rolled"]:
            print("[upgrade_check] pin matches. No change since last --update-pin.")
        else:
            print(f"[upgrade_check] CHANGED (pin: {pinned}, now: {current['_rolled']})")
            print("             Per-file detail not stored in pin; re-pin with")
            print("             --update-pin after reviewing the upstream changelog.")
        return 0

    if pinned != current["_rolled"]:
        print("[upgrade_check] MISMATCH - the openalgo/ folder differs from the pinned version.")
        print("             Run --update-pin to accept the new version, or restore")
        print("             the pinned version of openalgo/ from your backup.")
        return 2 if args.strict else 0

    print("[upgrade_check] OK - folder matches pin.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
