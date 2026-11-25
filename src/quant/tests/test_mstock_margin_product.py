"""mStock product mapping for margin delivery (owner 2026-11-25, vendored OpenAlgo change).

MTF maps to the broker's MARGIN type. An unknown product is refused, never sent as INTRADAY.
The vendored module imports its database and logging layer at load time. Those three names are
stubbed only while the file loads, then removed, so no database engine or vendored dependency is needed.

Flow stage: Adapters (broker product mapping)
Status: PHASE 1 IMPLEMENTED
"""
import importlib.util
import logging
import pathlib
import sys
import types

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[3]
TRANSFORM = ROOT / "src/quant/openalgo_platform/broker/mstock/mapping/transform_data.py"
STUBS = {
    "database.symbol": {"SymToken": object},
    "database.token_db": {"get_br_symbol": lambda *a, **k: None, "get_token": lambda *a, **k: None},
    "utils.logging": {"get_logger": logging.getLogger},
}


def _load(path, name):
    saved = {n: sys.modules.get(n) for n in STUBS}
    try:
        for n, attrs in STUBS.items():
            mod = types.ModuleType(n)
            mod.__dict__.update(attrs)
            sys.modules[n] = mod
        spec = importlib.util.spec_from_file_location(name, path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    finally:
        for n, prev in saved.items():
            if prev is None:
                sys.modules.pop(n, None)
            else:
                sys.modules[n] = prev


_mapping = _load(TRANSFORM, "mstock_transform_under_test")
map_product_type = _mapping.map_product_type
reverse_map_product_type = _mapping.reverse_map_product_type
_margin = _load(ROOT / "src/quant/openalgo_platform/broker/mstock/mapping/margin_data.py", "mstock_margin_under_test")
margin_map_product_type = _margin.map_product_type


@pytest.mark.parametrize("ours, theirs", [("CNC", "DELIVERY"), ("NRML", "CARRYFORWARD"),
                                          ("MIS", "INTRADAY"), ("MTF", "MARGIN")])
def test_outbound_product_map(ours, theirs):
    assert map_product_type(ours) == theirs


@pytest.mark.parametrize("bad", ["", None, "MARGIN", "mtf", "BTST"])
def test_unknown_product_refused_not_intraday(bad):
    with pytest.raises(ValueError, match="refused"):
        map_product_type(bad)


@pytest.mark.parametrize("theirs, ours", [("DELIVERY", "CNC"), ("CARRYFORWARD", "NRML"),
                                          ("INTRADAY", "MIS"), ("MARGIN", "MTF")])
def test_inbound_product_map(theirs, ours):
    assert reverse_map_product_type(theirs) == ours


@pytest.mark.parametrize("ours, theirs", [("CNC", "DELIVERY"), ("NRML", "CARRYFORWARD"),
                                          ("MIS", "INTRADAY"), ("MTF", "MARGIN")])
def test_margin_calculator_product_map(ours, theirs):
    # the margin calculator must price an MTF order as MARGIN, not as an intraday order
    assert margin_map_product_type(ours) == theirs


@pytest.mark.parametrize("bad", ["", None, "MARGIN", "BTST"])
def test_margin_calculator_refuses_unknown_product(bad):
    with pytest.raises(ValueError, match="refused"):
        margin_map_product_type(bad)
