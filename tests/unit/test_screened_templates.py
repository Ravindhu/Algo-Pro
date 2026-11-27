"""backup_state --setup creates data/screened/ from its templates (owner request 2026-11-27).

Rules under test:
1. A fresh folder gets data/screened/ with README, manual_symbols.json and source_mode.json.
2. The template defaults are the safe ones: no manual symbols, both switches off (both paths work).
3. An existing file is never overwritten, and a second setup changes nothing.
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "ops"))

import backup_state  # noqa: E402


def test_setup_creates_screened_folder_from_templates(tmp_path):
    report = backup_state.setup(tmp_path)
    screened = tmp_path / "data" / "screened"
    assert screened.is_dir()
    assert (screened / "README.md").is_file()
    assert json.loads((screened / "manual_symbols.json").read_text(encoding="utf-8")) == {"symbols": []}
    assert json.loads((screened / "source_mode.json").read_text(encoding="utf-8")) == {
        "manual": False, "automation": False}
    assert any(p.endswith("source_mode.json") for p in report["created"])


def test_setup_never_overwrites_and_is_repeatable(tmp_path):
    backup_state.setup(tmp_path)
    mine = tmp_path / "data" / "screened" / "manual_symbols.json"
    mine.write_text(json.dumps({"symbols": ["INFY"]}), encoding="utf-8")
    second = backup_state.setup(tmp_path)
    assert json.loads(mine.read_text(encoding="utf-8")) == {"symbols": ["INFY"]}
    assert not any("manual_symbols.json" in p for p in second["created"])
