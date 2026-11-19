"""Owner hard caps apply at the v5 risk gate, for every caller (audit item 5).

Before this change, the owner's config/risk_limits.yaml only reached the blue
runner's merge. Any other path through `risk.gate.check_intent` traded under
looser caps. Now the gate itself loads and enforces them, so no caller can skip
them.
"""
from __future__ import annotations

import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "src" / "quant"))

from quant.core import owner_limits_store  # noqa: E402
from quant.core.config import RiskLimits  # noqa: E402
from quant.core.types import TradeIntent  # noqa: E402
from quant.monitor.safe_mode import Mode, SafeMode  # noqa: E402
from quant.risk.gate import RiskState, check_intent  # noqa: E402
from quant.risk.kill_switch import KillSwitch  # noqa: E402

LOOSE = RiskLimits(
    max_order_quantity=1000, max_order_notional=1_000_000.0,
    max_open_exposure=2_000_000.0, max_daily_loss=50_000.0,
    max_strategy_loss=10_000.0, max_adv_pct=0.05, max_symbols_per_portfolio=10,
)


def intent(qty=10, price=1000.0, **kw):
    base = dict(intent_id="I1", strategy_id="S1", symbol="RELIANCE", side="BUY",
                qty=qty, ref_price=price, kind="entry", lot_size=1,
                prev_close=price * 0.99, circuit_pct=0.2, adv_shares=1e6,
                asm_gsm=False, trade_day=None)
    base.update(kw)
    return TradeIntent(**base)


@pytest.fixture()
def kill(tmp_path):
    return KillSwitch(tmp_path / "kill.json")


@pytest.fixture()
def safe():
    return SafeMode()


@pytest.fixture()
def state():
    return RiskState()


def _owner_file(tmp_path, body):
    p = tmp_path / "risk_limits.yaml"
    p.write_text(body, encoding="utf-8")
    return p


# --------------------------------------------------------------------------- #
# owner_limits_store
# --------------------------------------------------------------------------- #
def test_store_reads_the_set_caps_and_skips_nulls(tmp_path):
    p = _owner_file(tmp_path, "max_order_quantity: 10\nmax_order_notional: null\n"
                             "blocked: false\n")
    owner = owner_limits_store.load(p)
    assert owner["max_order_quantity"] == 10
    assert "max_order_notional" not in owner   # null = not set
    assert owner["blocked"] is False


def test_store_fails_closed_on_an_unreadable_file(tmp_path):
    owner = owner_limits_store.load(tmp_path / "missing.yaml")
    assert owner == owner_limits_store.BLOCKED
    assert owner["blocked"] is True


def test_store_does_not_cache_a_failed_read(tmp_path):
    owner_limits_store.reset()
    missing = tmp_path / "missing.yaml"
    assert owner_limits_store.load(missing)["blocked"] is True
    # The file appears: the next load must pick it up, not the cached failure.
    _owner_file(tmp_path, "max_order_quantity: 10\n")
    assert owner_limits_store.load(missing)["max_order_quantity"] == 10


# --------------------------------------------------------------------------- #
# the gate refuses an entry over an owner cap
# --------------------------------------------------------------------------- #
def test_owner_cap_overrides_a_looser_config(tmp_path, kill, safe, state):
    p = _owner_file(tmp_path, "max_order_quantity: 10\n")
    d = check_intent(intent(qty=25), state, LOOSE, kill, safe,
                     owner_limits=owner_limits_store.load(p))
    assert d.approved is False
    assert any("owner limit: max_order_quantity" in r for r in d.reasons)


def test_owner_notional_cap_applies_to_qty_times_price(tmp_path, kill, safe, state):
    p = _owner_file(tmp_path, "max_order_notional: 10000\n")
    d = check_intent(intent(qty=15, price=1000.0), state, LOOSE, kill, safe,
                     owner_limits=owner_limits_store.load(p))
    assert d.approved is False
    assert any("max_order_notional" in r and "owner limit" in r for r in d.reasons)


def test_intent_inside_the_owner_caps_passes(tmp_path, kill, safe, state):
    p = _owner_file(tmp_path, "max_order_quantity: 10\nmax_order_notional: 20000\n")
    d = check_intent(intent(qty=10, price=1000.0), state, LOOSE, kill, safe,
                     owner_limits=owner_limits_store.load(p))
    assert d.approved is True


def test_null_owner_cap_does_not_invent_a_limit(tmp_path, kill, safe, state):
    p = _owner_file(tmp_path, "max_order_quantity: null\nmax_order_notional: null\n")
    d = check_intent(intent(qty=25), state, LOOSE, kill, safe,
                     owner_limits=owner_limits_store.load(p))
    assert d.approved is True
    assert not any("owner limit" in r for r in d.reasons)


def test_unreadable_owner_file_refuses_the_entry(tmp_path, kill, safe, state):
    d = check_intent(intent(), state, LOOSE, kill, safe,
                     owner_limits=owner_limits_store.load(tmp_path / "no.yaml"))
    assert d.approved is False
    assert any("owner limits" in r for r in d.reasons)


def test_blocked_true_in_the_owner_file_refuses_the_entry(tmp_path, kill, safe, state):
    p = _owner_file(tmp_path, "blocked: true\n")
    d = check_intent(intent(), state, LOOSE, kill, safe,
                     owner_limits=owner_limits_store.load(p))
    assert d.approved is False
    assert any("blocked=true" in r for r in d.reasons)


def test_exits_are_not_blocked_by_an_owner_quantity_cap(tmp_path, kill, safe, state):
    """Exits reduce risk: per-order caps never apply to them."""
    p = _owner_file(tmp_path, "max_order_quantity: 10\n")
    ex = intent(qty=500, side="SELL", kind="exit")
    d = check_intent(ex, state, LOOSE, kill, safe,
                     owner_limits=owner_limits_store.load(p))
    assert d.approved is True


def test_gate_enforces_owner_caps_without_an_explicit_argument(kill, safe, state):
    """The default path reads the repo's own config/risk_limits.yaml (cap 10)."""
    owner_limits_store.reset()
    d = check_intent(intent(qty=999), state, LOOSE, kill, safe)
    assert d.approved is False
    assert any("owner limit: max_order_quantity" in r for r in d.reasons)


# --------------------------------------------------------------------------- #
# pretrade backstop
# --------------------------------------------------------------------------- #
def test_pretrade_checks_owner_caps_directly():
    from quant.risk.pretrade import pretrade_checks
    reasons = pretrade_checks(intent(qty=25), LOOSE,
                              owner_limits={"max_order_quantity": 10})
    assert any("owner limit: max_order_quantity" in r for r in reasons)


def test_pretrade_ignores_non_numeric_owner_values():
    from quant.risk.pretrade import pretrade_checks
    reasons = pretrade_checks(intent(), LOOSE,
                              owner_limits={"max_order_quantity": "garbage"})
    assert not any("owner limit" in r for r in reasons)
