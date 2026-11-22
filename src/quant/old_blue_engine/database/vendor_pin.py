"""Pin and safely upgrade the optional Indian market-history research checkout.

The upstream clone is pristine and sparse: the data/ directory is not
materialized. Engine integrations belong in database/ adapters, never upstream.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
VENDOR_DIR = HERE / "vendor" / "indian-market-historical-data"
PIN_FILE = HERE / "vendor" / "INDIAN_MARKET_DATA_UPSTREAM_PIN"
UPSTREAM_URL = "https://github.com/aswinv90/indian-market-historical-data.git"
GIT_CANDIDATES = [r"C:\Program Files\Git\cmd\git.exe", "git"]


def _git(*args: str) -> tuple[int, str]:
    """Run Git against the vendor checkout and return its status and output."""
    last = (1, "git executable not found")
    for executable in GIT_CANDIDATES:
        try:
            result = subprocess.run(
                [executable, *args],
                cwd=str(VENDOR_DIR),
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            )
            return result.returncode, (result.stdout or result.stderr).strip()
        except FileNotFoundError:
            last = (1, f"git not found at {executable}")
        except subprocess.TimeoutExpired:
            return 1, "git command timed out"
    return last


def read_pin() -> str:
    if not PIN_FILE.is_file():
        return ""
    for line in PIN_FILE.read_text(encoding="utf-8").splitlines():
        value = line.strip()
        if value and not value.startswith("#"):
            return value
    return ""


def _state() -> tuple[str, str, bool]:
    rc, head = _git("rev-parse", "HEAD")
    if rc:
        return "", "", True
    rc, origin = _git("remote", "get-url", "origin")
    if rc:
        return head, "", True
    rc, dirty = _git("status", "--porcelain", "--untracked-files=all")
    return head, origin, rc != 0 or bool(dirty)


def _expected_remote(origin: str) -> bool:
    return origin.rstrip("/").removesuffix(".git").casefold() == (
        UPSTREAM_URL.removesuffix(".git").casefold()
    )


def do_check() -> int:
    if not VENDOR_DIR.is_dir():
        print(f"[FAIL] vendor checkout missing: {VENDOR_DIR}")
        return 1
    head, origin, dirty = _state()
    pinned = read_pin()
    if not head or not _expected_remote(origin):
        print(f"[FAIL] unexpected or unreadable upstream remote: {origin or '(none)'}")
        return 1
    if not pinned or head != pinned:
        print(f"[DRIFT] pinned {pinned or '(none)'}; checkout is {head}")
        print("        review upstream changes before updating the pin")
        return 1
    if dirty:
        print("[FAIL] vendor checkout is dirty; keep upstream files pristine")
        return 1
    if (VENDOR_DIR / "data").exists():
        print("[FAIL] upstream data/ is checked out; remove it from sparse checkout")
        return 1
    print(f"[OK] pristine upstream checkout matches pin {head}")
    print("[OK] upstream data/ remains unmaterialized")
    return 0


def do_diff() -> int:
    head, origin, dirty = _state()
    print(f"vendor   : {VENDOR_DIR}")
    print(f"upstream : {origin or '(unreadable)'}")
    print(f"HEAD     : {head or '(unreadable)'}")
    print(f"pinned   : {read_pin() or '(none)'}")
    print(f"dirty    : {dirty}")
    print(f"data/ checked out: {(VENDOR_DIR / 'data').exists()}")
    return 0 if head else 1


def do_update_pin() -> int:
    if not VENDOR_DIR.is_dir():
        print(f"[FAIL] vendor checkout missing: {VENDOR_DIR}")
        return 1
    before, origin, dirty = _state()
    if not _expected_remote(origin):
        print(f"[FAIL] unexpected upstream remote: {origin or '(none)'}")
        return 1
    if dirty:
        print("[FAIL] vendor checkout has local changes; refusing to update")
        return 1

    rc, output = _git("pull", "--ff-only")
    if rc:
        print(f"[FAIL] git pull --ff-only failed:\n{output}")
        print("       resolve divergence manually; no merge was attempted")
        return 1
    after, _, dirty = _state()
    if not after or dirty:
        print("[FAIL] checkout is unreadable or dirty after pull; pin unchanged")
        return 1
    PIN_FILE.write_text(
        "# Upstream: https://github.com/aswinv90/indian-market-historical-data\n"
        "# License: personal, non-commercial, educational research only.\n"
        f"{after}\n",
        encoding="utf-8",
    )
    if before == after:
        print(f"[OK] already current; pin verified at {after}")
    else:
        print(f"[UPDATED] {before} -> {after}")
        print("[NEXT] review upstream changes before using any updated code or data")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Check or fast-forward the pinned market-history research checkout."
    )
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument("--check", action="store_true")
    actions.add_argument("--diff", action="store_true")
    actions.add_argument("--update-pin", action="store_true")
    args = parser.parse_args()
    if args.update_pin:
        return do_update_pin()
    if args.diff:
        return do_diff()
    return do_check()


if __name__ == "__main__":
    sys.exit(main())
