"""Guard the security floor for PDF parsing.

pypdf parses PDFs downloaded from the web (annual reports). A loose floor such
as ``pypdf>=5.0`` lets pip install old releases, so the floor in pyproject must
never be lower than the pin in the repo-level requirements file.
"""
from __future__ import annotations

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[2]
PYPROJECT = ROOT / "Blue_engine" / "pyproject.toml"
REQUIREMENTS = ROOT / "requirements.txt"


def _version(text: str) -> tuple:
    return tuple(int(part) for part in text.split("."))


def test_pypdf_floor_is_not_below_pinned_version():
    pinned = re.search(r"^pypdf==([\d.]+)\s*$", REQUIREMENTS.read_text(),
                       re.M)
    floor = re.search(r'"pypdf>=([\d.]+)"', PYPROJECT.read_text())
    assert pinned and floor, "pypdf pin or floor not found"
    assert _version(floor.group(1)) >= _version(pinned.group(1)), (
        f"pyproject floor pypdf>={floor.group(1)} allows versions below the "
        f"pinned {pinned.group(1)}")
