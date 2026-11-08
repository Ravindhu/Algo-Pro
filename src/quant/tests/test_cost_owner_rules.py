"""X-5: Indian cost model checked against the owner-confirmed rules (2026-11-03).

Flow stage: Indian cost model
Status: TEST

Expected rupee amounts are hand-calculated here, not taken from the code.
They run without OpenAlgo or Blue_engine (the cost model is local since stage 2).

Rules (see src/quant/research/costs.py):
- Delivery: brokerage 0. STT 0.1% on both legs. Stamp 0.015% on buy only.
- Intraday: brokerage 0.03% capped at Rs 20 per order. STT 0.025% on sell only.
  Stamp 0.003% on buy only.
- All: exchange 0.00307% (NSE circular FA73061, eff. 1 Mar 2026), SEBI 0.0001%, GST 18% on brokerage+exchange+SEBI only,
  flat impact 5 bps per side.
"""
import pytest

from quant.research.costs import CostModel

REL = 1e-9
ABS = 1e-6


@pytest.fixture
def cm():
    return CostModel()


# Hand-calculated totals in rupees (notional, side, product, expected).
HAND_TOTALS = [
    # Delivery buy: STT 100 + txn 3.22 + SEBI 0.1 + GST 0.5976 + stamp 15 + impact 50
    (100_000, "BUY", "delivery", 168.7406),
    # Delivery sell: no stamp; STT still charged on sell
    (100_000, "SELL", "delivery", 153.7406),
    # Intraday buy, brokerage cap binds (0.03% = Rs 30 > Rs 20): 20 + 3.22 + 0.1 + 4.1976 + 3 + 50
    (100_000, "BUY", "intraday", 80.3406),
    # Intraday sell: brokerage 20 + STT 25 (sell only) + txn + SEBI + GST + impact
    (100_000, "SELL", "intraday", 102.3406),
    # Intraday buy, cap does not bind (0.03% = Rs 18): brokerage 18 + ... + stamp 1.8 + impact 30
    (60_000, "BUY", "intraday", 55.28436),
    # Intraday buy, small order, no cap: 0.3 + 0.0322 + 0.001 + 0.059976 + 0.03 + 0.5
    (1_000, "BUY", "intraday", 0.921406),
    # Zero notional costs nothing
    (0, "BUY", "delivery", 0.0),
]


@pytest.mark.parametrize("notional,side,product,expected", HAND_TOTALS)
def test_hand_calculated_totals(cm, notional, side, product, expected):
    assert cm.trade_cost(notional, side, product) == pytest.approx(expected, rel=REL, abs=ABS)


def test_delivery_brokerage_is_zero(cm):
    # Only the sell-side gap should be stamp-free: delivery buy - sell = stamp duty exactly.
    gap = cm.trade_cost(100_000, "BUY", "delivery") - cm.trade_cost(100_000, "SELL", "delivery")
    assert gap == pytest.approx(0.00015 * 100_000, rel=REL, abs=ABS)


def test_intraday_stt_is_on_sell_leg_only(cm):
    # Intraday sell - buy = STT sell (25) - stamp buy (3); brokerage and GST are the same both sides.
    gap = cm.trade_cost(100_000, "SELL", "intraday") - cm.trade_cost(100_000, "BUY", "intraday")
    assert gap == pytest.approx(0.00025 * 100_000 - 0.00003 * 100_000, rel=REL, abs=ABS)


def test_brokerage_cap_is_exactly_twenty_rupees(cm):
    # Rs 1 crore intraday buy: brokerage stays at Rs 20 (0.03% would be Rs 3000).
    # 20 + 322 (txn) + 10 (SEBI) + 63.36 (GST on 20+322+10) + 300 (stamp) + 5000 (impact)
    assert cm.trade_cost(10_000_000.0, "BUY", "intraday") == pytest.approx(5697.66, rel=REL, abs=ABS)


def test_gst_applies_to_brokerage_txn_and_sebi_only(cm):
    # Delivery sell has no brokerage. GST = 18% of (txn + SEBI), not of STT or impact.
    notional = 100_000
    total = cm.trade_cost(notional, "SELL", "delivery")
    without_gst = notional * (0.001 + 0.0000307 + 0.000001 + 0.0005)
    assert total - without_gst == pytest.approx(0.18 * notional * (0.0000307 + 0.000001), rel=REL, abs=ABS)


def test_impact_is_flat_five_bps_per_side():
    notional = 250_000
    with_impact = CostModel(impact_bps=5.0).trade_cost(notional, "BUY", "intraday")
    no_impact = CostModel(impact_bps=0.0).trade_cost(notional, "BUY", "intraday")
    assert with_impact - no_impact == pytest.approx(0.0005 * notional, rel=REL, abs=ABS)
    with_impact_s = CostModel(impact_bps=5.0).trade_cost(notional, "SELL", "intraday")
    no_impact_s = CostModel(impact_bps=0.0).trade_cost(notional, "SELL", "intraday")
    assert with_impact_s - no_impact_s == pytest.approx(0.0005 * notional, rel=REL, abs=ABS)


def test_cost_is_never_negative(cm):
    for side in ("BUY", "SELL"):
        for product in ("delivery", "intraday"):
            assert cm.trade_cost(1_000.0, side, product) >= 0.0


@pytest.mark.parametrize("bad_side", ["buy", "sell", "Buy", "", "SHORT"])
def test_unknown_side_is_refused(bad_side):
    # No Blue needed: validation runs before any arithmetic.
    with pytest.raises(ValueError):
        CostModel().trade_cost(1_000.0, bad_side, "delivery")


@pytest.mark.parametrize("bad_product", ["Intraday", "MIS", "cnc", ""])
def test_unknown_product_is_refused(bad_product):
    with pytest.raises(ValueError):
        CostModel().trade_cost(1_000.0, "BUY", bad_product)


def test_negative_notional_is_refused():
    with pytest.raises(ValueError):
        CostModel().trade_cost(-1.0, "BUY", "delivery")
