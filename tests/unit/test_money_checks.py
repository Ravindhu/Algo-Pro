"""Hand-checked tests for the ported money checks (no Blue_engine needed).

Flow stage: Risk gate (monetary breach checks)
Status: PHASE 1 IMPLEMENTED
Parity against Blue's RiskGate lives in tests/parity/test_money_checks_parity.py.
"""
from quant.core.config import RiskLimits
from quant.core.types import TradeIntent
from quant.risk.money_checks import entry_breaches, money_breaches

LIMITS = RiskLimits(max_order_quantity=100, max_order_notional=50_000.0,
                    max_open_exposure=200_000.0, max_daily_loss=5_000.0,
                    max_strategy_loss=2_000.0, max_adv_pct=0.05)


def _intent(**kw):
    base = dict(intent_id="I", strategy_id="S", symbol="X", side="BUY", qty=10,
                ref_price=100.0, kind="entry", lot_size=1, adv_shares=1e6)
    base.update(kw)
    return TradeIntent(**base)


def _breaches(intent, open_notional=0.0, daily_pnl=0.0, limits=LIMITS):
    return entry_breaches(intent, limits, open_notional=open_notional, daily_pnl=daily_pnl)


def test_small_entry_passes():
    assert _breaches(_intent()) == []


def test_quantity_above_cap_is_a_breach():
    assert _breaches(_intent(qty=101)) == ["quantity 101 > max_order_quantity 100"]


def test_quantity_at_cap_passes():
    assert _breaches(_intent(qty=100, ref_price=100.0)) == []


def test_notional_above_cap_is_a_breach():
    assert _breaches(_intent(qty=60, ref_price=1000.0)) == [
        "notional 60000.00 > max_order_notional 50000.00"]


def test_daily_loss_at_limit_blocks_entry():
    assert _breaches(_intent(), daily_pnl=-5_000.0) == [
        "day_pnl -5000.00 <= -max_daily_loss 5000.00"]


def test_daily_loss_just_inside_limit_passes():
    assert _breaches(_intent(), daily_pnl=-4_999.0) == []


def test_exposure_exactly_at_cap_passes():
    # open 190,000 + 10 x 1,000 = 200,000 == cap. The rule is strictly greater, so it passes.
    assert _breaches(_intent(qty=10, ref_price=1000.0), open_notional=190_000.0) == []


def test_exposure_projection_over_cap_is_a_breach():
    # order 30 x 1,000 = 30,000 (inside the 50,000 notional cap); 180,000 + 30,000 = 210,000 > 200,000
    assert _breaches(_intent(qty=30, ref_price=1000.0), open_notional=180_000.0) == [
        "projected exposure 210000.00 > max_open_exposure 200000.00"]


def test_zero_price_fails_closed_when_notional_is_capped():
    assert _breaches(_intent(ref_price=0.0)) == [
        "cannot evaluate max_order_notional without a finite positive order price",
        "cannot evaluate max_open_exposure without a finite positive order price",
    ]


def test_unverified_pnl_fails_closed():
    assert _breaches(_intent(), daily_pnl=float("nan")) == [
        "cannot evaluate max_daily_loss without a verified daily P&L value"]


def test_unverified_exposure_fails_closed():
    assert _breaches(_intent(), open_notional=float("nan")) == [
        "cannot evaluate max_open_exposure without verified current exposure"]


def test_zero_quantity_is_a_breach():
    assert "quantity 0 must be a positive integer" in _breaches(_intent(qty=0))


def test_unknown_action_is_a_breach_from_the_core_function():
    out = money_breaches("HOLD", 10, 100.0, max_quantity=None, max_notional=None,
                         max_daily_loss=None, max_exposure=None, day_pnl=None, open_exposure=None)
    assert out == ["action 'HOLD' not in allowed_actions ['BUY', 'SELL']"]


def test_exits_are_never_checked():
    assert _breaches(_intent(kind="exit", side="SELL", qty=10_000),
                     open_notional=9e9, daily_pnl=-9e9) == []


def test_unset_limits_are_not_checked_here():
    """The 'limit not set' policy lives in pretrade.py and loss_breaker.py."""
    assert _breaches(_intent(qty=10_000), limits=RiskLimits()) == []


def test_non_finite_numbers_count_as_missing():
    out = money_breaches("BUY", 10, float("inf"), max_quantity=None, max_notional=5000.0,
                         max_daily_loss=None, max_exposure=None, day_pnl=None, open_exposure=None)
    assert out == ["cannot evaluate max_order_notional without a finite positive order price"]


def test_money_checks_has_no_blue_import():
    import ast
    import pathlib
    import quant.risk.money_checks as mod
    tree = ast.parse(pathlib.Path(mod.__file__).read_text(encoding="utf-8"))
    names = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            names.append(node.module or "")
    assert not [n for n in names if "blue" in n.lower()]
