"""X-29: code must not hard-code the lowercase folder name `blue_engine/` as a path.

The folder on disk is `Blue_engine/` (capital B). Linux is case-sensitive, so a quoted
path such as "blue_engine/input/x.json" fails at run time. Module imports
(`blue_engine.core`) are fine: the bridge maps them. Comments and docstrings are fine too.
Flow stage: none (repo hygiene). Status: TEST.
"""
import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
#: Linux (the VPS) is case-sensitive; Windows and macOS are not. On a
#: case-insensitive filesystem ``ROOT / "blue_engine"`` resolves to the real
#: ``Blue_engine/`` folder no matter how it is written, so the on-disk spelling
#: cannot be tested there at all. The literal-path check below still runs
#: everywhere and is what actually protects the Linux server (CI is ubuntu).
CASE_SENSITIVE_FS = os.name != "nt"
#: A quote right before the lowercase folder name means a path literal.
PATH_LITERAL = re.compile(r"""["']blue_engine/""")


def test_no_quoted_lowercase_blue_engine_path_in_code():
    offenders = []
    for base in (ROOT / "src", ROOT / "Blue_engine"):
        for py in base.rglob("*.py"):
            if "tests" in py.parts or "fixtures" in py.parts:
                continue
            text = py.read_text(encoding="utf-8", errors="replace")
            for number, line in enumerate(text.splitlines(), 1):
                if PATH_LITERAL.search(line):
                    offenders.append(f"{py.relative_to(ROOT)}:{number}")
    assert offenders == [], "lowercase blue_engine/ path literal in: " + ", ".join(offenders)


def test_folder_on_disk_is_capitalised():
    assert (ROOT / "Blue_engine" / "__init__.py").is_file()
    if not CASE_SENSITIVE_FS:
        return  # Windows/macOS cannot express the difference; see CASE_SENSITIVE_FS
    assert not (ROOT / "blue_engine").exists()
