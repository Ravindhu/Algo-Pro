"""The flow doc must not claim things the code contradicts.

Documentation drifted silently for weeks: the doc said "seven Blue modules
wrapped" when 32 were, "no screener writer yet" when one existed, "OCL strategies
are not in the bot" when three were selectable, and "DV-ATR is not implemented"
when it was fully implemented across two strategy files.

Every one of those was a claim about REALITY that a machine can check. These tests
check them, so the doc cannot rot again without a red test.
"""
import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
DOC = ROOT / "docs" / "quant_system_flow_v5.md"
AGENTS = ROOT / "AGENTS.md"
SRC = ROOT / "src" / "quant"


def _doc() -> str:
    return DOC.read_text(encoding="utf-8")


def _doc_live() -> str:
    """The doc with ~~struck-through~~ text removed.

    Strikethrough is a historical record of a fixed claim ("~~no screener writer
yet.~~ Fixed..."). Deleting it would erase the audit trail, so the consistency
    checks read the doc WITHOUT struck text: a live claim must be accurate, a
    retired one is allowed to say what used to be wrong.
    """
    return re.sub(r"~~.*?~~", "", _doc())


def _agents() -> str:
    return AGENTS.read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# paths the doc claims must exist
# --------------------------------------------------------------------------- #
def test_every_wrapper_path_in_the_doc_exists():
    """A doc that names a file which does not exist is worse than no doc."""
    missing = []
    for line in _doc().splitlines():
        if not line.startswith("| `"):
            continue
        cells = [c.strip() for c in line.split("|")]
        if len(cells) < 4:
            continue
        for token in re.findall(r"`([^`]+)`", cells[2]):
            if "(" in token or not (token.endswith(".py") or token.endswith("/")):
                continue
            base = token.split(" ")[0]
            if not ((SRC / base).exists() or (SRC / f"{base}.py").exists()):
                missing.append(base)
    assert missing == [], f"doc names wrapper paths that do not exist: {missing}"


# --------------------------------------------------------------------------- #
# claims the code can settle
# --------------------------------------------------------------------------- #
def test_screener_writer_claim_is_accurate():
    """The doc used to say no writer existed."""
    writer = SRC / "data" / "screener_engine.py"
    text = writer.read_text(encoding="utf-8")
    assert "def write_screen_file" in text
    assert "no screener writer yet" not in _doc_live().lower()


def test_ocl_strategies_claim_is_accurate():
    """The doc used to say OCL strategies were not in the bot."""
    adapter = SRC / "strategies" / "ocl" / "adapter.py"
    assert adapter.is_file()
    assert "ocl strategies are not in the bot" not in _doc_live().lower()


def test_dv_atr_claim_is_accurate():
    """The doc used to say DV-ATR was not implemented; it is, in the strategies."""
    strategy = SRC / "strategies" / "ocl" / "ocl_sell_fail_top.py"
    text = strategy.read_text(encoding="utf-8")
    assert "DV-ATR" in text or "_dvs_" in text
    assert "DV-ATR is not implemented" not in _doc_live()
    # But exit_manager must NOT apply it: DV-ATR is the strategy's own management.
    exit_mgr = (SRC / "execution" / "exit_manager.py").read_text(encoding="utf-8")
    assert "_dvs_stop" not in exit_mgr


def test_pnl_persistence_claim_is_accurate():
    """The doc used to say counters reset on restart."""
    assert (SRC / "execution" / "pnl_store.py").is_file()
    assert "counters reset to zero on process restart" not in _doc_live().lower()


def test_wrapped_module_count_matches_reality():
    """The doc states a count; it must be the real one."""
    # Walk only the quant lanes that actually call the bridge. A full rglob of
    # src/quant would include the vendored OpenAlgo platform and take minutes.
    loaded = set()
    for lane in ("core", "data", "models", "portfolio", "research", "risk",
                 "execution", "monitor", "strategies", "feedback", "reporting",
                 "notify", "audit"):
        folder = SRC / lane
        if not folder.is_dir():
            continue
        for path in folder.rglob("*.py"):
            if "blue_engine" in path.parts or "tests" in path.parts:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            loaded.update(re.findall(r'load\("([^"]+)"\)', text))
    loaded.discard("does_not_exist_module")     # deliberate bridge-test fixture

    # The doc states the count as a NUMBER ("**28** distinct Blue modules"). A
    # billboard, so it is compared as a number rather than a spelled-out word.
    match = re.search(r"\*\*(\d+)\*\* distinct Blue modules", _doc())
    assert match, "the doc must state the wrapped-module count as **N**"
    assert int(match.group(1)) == len(loaded), (
        f"doc states {match.group(1)} wrapped modules; {len(loaded)} are actually "
        f"loaded ({sorted(loaded)})")


# --------------------------------------------------------------------------- #
# the ten lanes must all be described
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("lane", [
    "Research validation", "Market inputs", "Data and features",
    "Signal and conviction", "Sizing and portfolio", "Portfolio risk",
    "Session guards", "Safety gate", "Execution", "Post-trade",
])
def test_all_ten_lanes_are_documented(lane):
    assert lane in _doc(), f"lane '{lane}' is missing from the flow doc"


def test_no_placeholder_stage_markers_remain():
    """A 'not implemented' claim must be one we have verified is still true."""
    text = _doc()
    for banned in ("TODO", "FIXME", "not built yet"):
        assert banned not in text, f"doc still carries '{banned}'"
