r"""
_vendor_pin.py - fingerprint + upgrade helper for the vendored screener-india clone.

Mirrors the role of ``scripts/upgrade_check.py`` for OpenAlgo, scoped to
``Blue_engine/screener/vendor/screener-india``.

WHY A PIN AT ALL
----------------
``vendor/screener-india/`` is a PRISTINE upstream clone. Our Python port in
``client/`` is written against a specific upstream commit; if the clone silently
moves (a ``git pull`` in the vendor dir, a re-clone, a different machine) the
port and the reference can drift apart with nobody noticing, because the port
does not import the TypeScript at all.

The pin closes that hole: it records the exact commit the port was written
against, and ``--check`` compares the live clone to it. This is deliberately the
same shape as ``openalgo.UPSTREAM_PIN`` so one mental model covers both.

USAGE
-----
    # Verify the clone matches the pin (exit 1 on drift)
    python Blue_engine/screener/_vendor_pin.py --check

    # Print the current commit + whether the port's watched files changed
    python Blue_engine/screener/_vendor_pin.py --diff

    # Pull upstream and re-point the pin at the new HEAD (after porting changes)
    python Blue_engine/screener/_vendor_pin.py --update-pin

SAFETY
------
* The vendor clone is never hand-edited by this script. It only reads git state,
  and ``--update-pin`` performs a ``git pull --ff-only`` - which aborts rather
  than merge on divergence.
* The pin FILE is the only thing rewritten, and only under ``--update-pin``.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
VENDOR_DIR = HERE / "vendor" / "screener-india"
PIN_FILE = HERE / "vendor" / "SCREENER_INDIA_UPSTREAM_PIN"
UPSTREAM_URL = "https://github.com/sahiljani/screener-india.git"

#: The upstream files the Python port is a translation of. If any of these move,
#: the port must be re-checked even when the commit hash alone would not tell us
#: which behaviour changed.
WATCHED = [
    "src/client/base.ts",
    "src/client/company.ts",
    "src/client/sectors.ts",
    "src/client/screens.ts",
    "src/client/search.ts",
    "src/client/index.ts",
    "src/utils/parser.ts",
    "src/utils/throttle.ts",
    "src/utils/cache.ts",
    "src/utils/retry.ts",
    "src/types.ts",
]

#: git is invoked through the known-good Windows path first (see AGENTS.md),
#: falling back to whatever `git` resolves to.
_GIT_CANDIDATES = [r"C:\Program Files\Git\cmd\git.exe", "git"]


def _git(*args: str, cwd: Path = VENDOR_DIR) -> tuple:
    """Run git and return ``(returncode, stdout)``. Never raises."""
    last = (1, "")
    for exe in _GIT_CANDIDATES:
        try:
            proc = subprocess.run(
                [exe, *args], cwd=str(cwd), capture_output=True, text=True,
                timeout=60,
            )
            return proc.returncode, (proc.stdout or "").strip()
        except FileNotFoundError:
            last = (1, f"git not found at {exe}")
            continue
        except Exception as exc:  # noqa: BLE001 - timeout, perms, ...
            last = (1, str(exc))
            continue
    return last


def read_pin() -> str:
    """Return the pinned commit from the pin file, or '' when absent."""
    if not PIN_FILE.is_file():
        return ""
    for line in PIN_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if len(line) >= 7 and all(c in "0123456789abcdef" for c in line.lower()):
            return line
    return ""


def current_commit() -> str:
    rc, out = _git("rev-parse", "HEAD")
    return out if rc == 0 else ""


def is_dirty() -> bool:
    rc, out = _git("status", "--porcelain")
    return rc == 0 and bool(out)


def do_check() -> int:
    """Exit 0 when the clone matches the pin, 1 otherwise."""
    if not VENDOR_DIR.is_dir():
        print(f"[FAIL] vendor clone missing at {VENDOR_DIR}")
        return 1
    pinned = read_pin()
    head = current_commit()
    if not head:
        print("[FAIL] could not read the vendor commit (is it a git clone?)")
        return 1
    if not pinned:
        print(f"[WARN] no pin recorded; current HEAD is {head[:12]}")
        print("       run --update-pin to record it")
        return 0
    if not head.startswith(pinned[:12]) and not pinned.startswith(head[:12]):
        print(f"[DRIFT] pinned {pinned[:12]} but clone is at {head[:12]}")
        print("        the Python port may no longer match the reference")
        return 1
    if is_dirty():
        print(f"[WARN] clone matches the pin ({head[:12]}) but has LOCAL CHANGES.")
        print("       vendor/ must stay pristine - move your edit into client/.")
        return 1
    print(f"[OK] vendor clone matches pin {head[:12]}")
    return 0


def do_diff() -> int:
    """Report the clone's commit, dirty state, and any watched-file drift."""
    head = current_commit()
    print(f"vendor   : {VENDOR_DIR}")
    print(f"upstream : {UPSTREAM_URL}")
    print(f"HEAD     : {head[:12] or '(unreadable)'}")
    print(f"pinned   : {read_pin()[:12] or '(none)'}")
    print(f"dirty    : {is_dirty()}")
    print("watched files:")
    for rel in WATCHED:
        path = VENDOR_DIR / rel
        print(f"  {'ok ' if path.is_file() else 'MISSING'} {rel}")
    return 0


def do_update_pin() -> int:
    """git pull --ff-only, then rewrite the pin to the new HEAD."""
    if not VENDOR_DIR.is_dir():
        print(f"[FAIL] vendor clone missing at {VENDOR_DIR}")
        return 1
    if is_dirty():
        print("[FAIL] vendor clone has local changes; refusing to pull.")
        print("       vendor/ must stay pristine. Move the edit into client/.")
        return 1

    before = current_commit()
    print(f"[pull] {before[:12]} -> upstream")
    rc, out = _git("pull", "--ff-only")
    if rc != 0:
        print(f"[FAIL] git pull --ff-only failed:\n{out}")
        print("       nothing was merged; resolve divergence manually.")
        return 1
    after = current_commit()
    if before == after:
        print("[ok] already up to date")
        return 0

    print(f"[pulled] {before[:12]} -> {after[:12]}")
    text = PIN_FILE.read_text(encoding="utf-8") if PIN_FILE.is_file() else ""
    lines = []
    replaced = False
    for line in text.splitlines():
        stripped = line.strip()
        if (stripped and not stripped.startswith("#")
                and len(stripped) >= 7
                and all(c in "0123456789abcdef" for c in stripped.lower())):
            lines.append(after)
            replaced = True
        else:
            lines.append(line)
    if not replaced:
        lines.append(after)
    PIN_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"[pin] {PIN_FILE.name} -> {after[:12]}")
    print("[NEXT] re-check Blue_engine/screener/client/*.py against the diff:")
    _git("log", "--oneline", f"{before}..{after}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(
        description="Pin/verify the vendored screener-india clone.")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--check", action="store_true",
                   help="Verify the clone matches the pin (default)")
    g.add_argument("--diff", action="store_true",
                   help="Print commit, dirty state and watched-file presence")
    g.add_argument("--update-pin", action="store_true",
                   help="git pull --ff-only upstream, then re-point the pin")
    args = p.parse_args()

    if args.update_pin:
        return do_update_pin()
    if args.diff:
        return do_diff()
    return do_check()


if __name__ == "__main__":
    sys.exit(main())
