"""Layout check: scaffold modules import and are marked as scaffolds.

Guards the rule that scaffold stubs carry no trade logic and are not wired
into the live path. See docs/quant_system_flow_v5.md.
"""
from __future__ import annotations

import importlib
import re
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[3]
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))


# Vendored OpenAlgo platform (decision 0013). It is upstream code, not our stage modules,
# so the stage rules below do not apply to it.
# Vendored code: the OpenAlgo platform (decision 0013) and the OpenAlgo SDK (decision 0014).
# The quant tests live inside the package (src/quant/tests/), so they are inside
# the tree these guards scan. They must be skipped: test_phase3.py and this file
# NAME the order client and the scaffold marker in order to assert the rules, so
# including them makes each guard fail on its own test. Production modules under
# src/quant/ are still fully checked, which is what the rules protect.
#
# ``blue_engine`` (moved to src/quant/blue_engine/ on 2026-11-14, decision 0032)
# is the EXISTING engine that the v5 stages wrap. It is not one of the v5 stage
# modules this file guards, it does not carry "Flow stage:" lines, and it owns
# its own order path through the SDK. Scanning it would make all three guards
# meaningless, so it is excluded exactly like the vendored trees.
VENDOR_DIRS = {"openalgo_platform", "vendor", "tests", "blue_engine"}


def _our_py_files():
    return [p for p in (SRC / "quant").rglob("*.py") if not VENDOR_DIRS & set(p.relative_to(SRC / "quant").parts)]


def _stage_files():
    """Stage modules only: package __init__ files and the vendored platform are excluded."""
    return [p for p in _our_py_files() if p.name != "__init__.py"]


def test_every_module_imports():
    for path in _our_py_files():
        rel = path.relative_to(SRC).with_suffix("")
        name = ".".join(rel.parts)
        if name.endswith(".__init__"):
            name = name[: -len(".__init__")]
        importlib.import_module(name)


def test_stage_modules_are_marked_scaffold():
    for path in _stage_files():
        assert re.search(r"^Status: (SCAFFOLD|PHASE \d IMPLEMENTED)", path.read_text(encoding="utf-8"), re.M), path


def test_only_openalgo_client_names_the_order_client():
    """The order call may be named only in execution/openalgo_client.py (vendored code is exempt; decision 0013).

    Checks both the method call and the raw endpoint path, so a transport call cannot hide the order route.
    """
    for path in _stage_files():
        if path.name == "openalgo_client.py":
            continue
        text = path.read_text(encoding="utf-8")
        assert ".placeorder(" not in text, path
        assert "placeorder" not in text.lower(), path


# SVG blocks with no module yet. Shrink this list as each gap gets a home. Empty = no gaps.
KNOWN_GAPS: set = {"Market engine"}  # Blue market engine: no quant lane loads it yet


def _svg_block_titles():
    import xml.etree.ElementTree as ET

    svg_ns = "{http://www.w3.org/2000/svg}"
    tree = ET.parse(ROOT / "quant_system_flow_v5.svg")
    # Font size 15 is used only for block titles in the SVG.
    return {t.text for t in tree.iter(svg_ns + "text") if t.get("font-size") == "15" and t.text}


def test_every_svg_block_is_mapped_to_a_module():
    """Each SVG block title must appear in a stage file's Flow stage line, or be a known gap."""
    titles = _svg_block_titles()
    assert len(titles) == 84, len(titles)  # guards against the parser finding nothing
    scripts = list((ROOT / "scripts").glob("*.py"))
    stage_lines = "\n".join(
        line
        for path in _stage_files() + scripts
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.startswith("Flow stage:")
    )
    missing = sorted(t for t in titles if t not in stage_lines and t not in KNOWN_GAPS)
    assert not missing, missing
    mapped_gaps = sorted(g for g in KNOWN_GAPS if g in stage_lines)
    assert not mapped_gaps, f"remove from KNOWN_GAPS: {mapped_gaps}"
