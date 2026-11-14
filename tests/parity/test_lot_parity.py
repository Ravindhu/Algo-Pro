"""Parity: src/quant/execution/lot.py vs Blue_engine/core/lot.py (decision 0022).

Blue's lot.py imports only typing at module level, so it loads here with no stubs.
The Blue master-DB and SDK lookups are not compared (they are not ported). They are
covered by the override and no-source checks below.

Flow stage: Algo router
Status: TEST
"""
import importlib.util
import pathlib
import sys

import pytest

from quant.execution import lot as ours

BLUE_LOT = pathlib.Path(__file__).resolve().parents[2] / "src" / "quant" / "blue_engine" / "core" / "lot.py"


@pytest.fixture(scope="module")
def blue():
    spec = importlib.util.spec_from_file_location("blue_lot_ref", BLUE_LOT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def _blue_db_importable():
    try:
        import database.token_db_enhanced  # noqa: F401
        return True
    except Exception:
        return False


QUANTITIES = [-50, -1, 0, 1, 2, 5, 7, 13, 1199, 1200, 1201, 2500, 10**9]
LOTS = [-4, -1, 0, 1, 5, 7, 1200]
OVERRIDES = [None, 0, -1, 1, 1200, "1200", 2.7, "abc", [], "-5"]


def test_lot_quantity_grid_matches_blue(blue):
    checked = 0
    for q in QUANTITIES:
        for lot in LOTS:
            assert ours.lot_quantity(q, lot) == blue.lot_quantity(q, lot), (q, lot)
            checked += 1
    assert checked == len(QUANTITIES) * len(LOTS)


@pytest.mark.parametrize("override", OVERRIDES)
def test_override_path_matches_blue(blue, override):
    a = ours.resolve_lot_size("INFY", "NSE", override=override)
    b = blue.resolve_lot_size(None, symbol="INFY", exchange="NSE", override=override)
    assert a == b, override


@pytest.mark.parametrize("symbol, exchange", [("INFY", "NSE"), ("", "NSE"), ("INFY", "")])
def test_no_source_gives_none_like_blue(blue, symbol, exchange):
    if _blue_db_importable():
        pytest.skip("OpenAlgo master DB is importable here; Blue would use it")
    assert ours.resolve_lot_size(symbol, exchange) is None
    assert blue.resolve_lot_size(None, symbol=symbol, exchange=exchange) is None
