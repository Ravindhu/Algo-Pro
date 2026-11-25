"""Guard for the local OpenAlgo margin patch (owner 2026-11-25).

The vendored copy under src/quant/openalgo_platform is NOT upstream. The upgrade pin does not cover these files,
so an upstream upgrade would drop the MTF support without any error. This test fails CI if a marker goes missing,
so the patch is re-applied (see docs/IMPLEMENTATION_PLAN_2026-11-25_MARGIN_MTF.md) before anything ships.

Flow stage: Adapters (broker product mapping)
Status: PHASE 3 IMPLEMENTED
"""
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[3]
V = ROOT / "src/quant/openalgo_platform"


def _text(rel):
    return (V / rel).read_text(encoding="utf-8")


def test_order_and_margin_schemas_accept_mtf():
    text = _text("restx_api/schemas.py")
    assert text.count('validate.OneOf(["MIS", "NRML", "CNC", "MTF"])') >= 2


def test_mstock_order_product_map_has_mtf_and_refuses_unknown():
    text = _text("broker/mstock/mapping/transform_data.py")
    assert '"MTF": "MARGIN"' in text and '"MARGIN": "MTF"' in text and "refused, no fallback" in text


def test_mstock_margin_calculator_maps_mtf_and_refuses_unknown():
    text = _text("broker/mstock/mapping/margin_data.py")
    assert '"MTF": "MARGIN"' in text and "refused, no fallback" in text


def test_mstock_trade_and_position_books_map_margin_to_mtf():
    text = _text("broker/mstock/mapping/order_data.py")
    assert 'trade["producttype"] = "MTF"' in text and 'position["producttype"] = "MTF"' in text
