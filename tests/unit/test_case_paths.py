"""X-29: code must not hard-code a stale ``Blue_engine/`` path segment.

History: the engine used to sit at the repo root as ``Blue_engine/`` (capital B)
while importing as ``blue_engine``. That mismatch is why this guard existed.
The tree now lives at ``src/quant/blue_engine/``, so the folder name and the
package name agree and case no longer decides whether a path resolves.

What still matters, and what this file now checks:
1. No code quotes a ``Blue_engine/`` path literal: that folder no longer exists,
   so such a path fails on every OS, not just Linux.
2. The engine package is on disk where the bridge expects it.
3. Nobody re-introduces a nested ``blue_engine/blue_engine`` wrapper folder.

PERFORMANCE: ``src/quant/openalgo_platform/`` carries its own ``.venv`` and a
``frontend/node_modules``. Walking them takes minutes and is never our code, so
they are pruned here. Without the prune this test hangs instead of failing,
which is worse than a red test.

Flow stage: none (repo hygiene). Status: TEST.
"""
import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ENGINE_DIR = ROOT / "src" / "quant" / "blue_engine"
#: A quote right before the old capitalised folder name means a dead path literal.
PATH_LITERAL = re.compile(r"""["']Blue_engine/""")
#: Directories that hold third-party installs, never our source.
PRUNE = {"node_modules", ".venv", "venv", "site-packages", "dist", "build", ".git", "tests", "fixtures"}


def _iter_py(base):
    """Yield .py files under ``base``, pruning vendored install trees and test folders."""
    if not base.is_dir():
        return
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = [d for d in dirnames if d not in PRUNE and not d.endswith(".egg-info")]
        for name in filenames:
            if name.endswith(".py"):
                yield Path(dirpath) / name


def test_no_quoted_blue_engine_path_literal_in_code():
    offenders = []
    for base in (ROOT / "src", ROOT / "scripts", ROOT / "dashboard"):
        for py in _iter_py(base):
            text = py.read_text(encoding="utf-8", errors="replace")
            for number, line in enumerate(text.splitlines(), 1):
                if PATH_LITERAL.search(line):
                    offenders.append(f"{py.relative_to(ROOT)}:{number}")
    assert offenders == [], "dead Blue_engine/ path literal in: " + ", ".join(offenders)


def test_engine_lives_under_src_quant():
    assert (ENGINE_DIR / "__init__.py").is_file()
    assert not (ROOT / "Blue_engine").exists()


def test_no_nested_engine_wrapper_folder():
    """A nested blue_engine/blue_engine would break package-dir and hide tests."""
    assert not (ENGINE_DIR / "blue_engine").exists()
