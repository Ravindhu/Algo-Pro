"""Flow stage: Strategy (OCL family, ported from Blue_engine ocl_keylevel).
Status: SMOKE TESTS WRITTEN; run result is in the commit message.

Checks the three ported OCL files: they import, each runs only its own blueprint,
the conformation entry model is gone, and a fetch failure returns no intent.
"""
import pytest

from quant.strategies.ocl.ocl import OclSignal
from quant.strategies.ocl.ocl_sell_fail_bottom import OclSellFailBottomSignal
from quant.strategies.ocl.ocl_sell_fail_top import OclSellFailTopSignal


class _FailingClient:
    """History call that always returns an error, so detect() finds no setup."""

    def history(self, **kwargs):
        return {"status": "error", "message": "test: no data"}


def _cfg(**extra):
    base = {"symbol": "INFY", "exchange": "NSE", "poll_seconds": 0,
            "htf_interval": "15m", "ltf_interval": "5m", "quantity": 1}
    base.update(extra)
    return base


@pytest.mark.parametrize("cls,mode", [
    (OclSignal, "OCL"),
    (OclSellFailTopSignal, "SELL_FAILURE_TOP"),
    (OclSellFailBottomSignal, "SELL_FAILURE_BOTTOM"),
])
def test_default_setup_mode_per_file(cls, mode):
    s = cls(_FailingClient(), _cfg())
    assert s.setup_mode == mode


@pytest.mark.parametrize("cls,wrong", [
    (OclSignal, "SELL_FAILURE_TOP"),
    (OclSellFailTopSignal, "OCL"),
    (OclSellFailBottomSignal, "SELL_FAILURE_TOP"),
])
def test_file_refuses_other_blueprint(cls, wrong):
    with pytest.raises(ValueError):
        cls(_FailingClient(), _cfg(setup_mode=wrong))


@pytest.mark.parametrize("cls", [OclSignal, OclSellFailTopSignal, OclSellFailBottomSignal])
def test_conformation_removed(cls):
    assert not hasattr(cls, "evaluate_bars")
    assert not hasattr(cls, "_conformation_detect")
    s = cls(_FailingClient(), _cfg())
    assert not hasattr(s, "entry_model")
    assert not hasattr(s, "rung_mode")


@pytest.mark.parametrize("cls", [OclSignal, OclSellFailTopSignal, OclSellFailBottomSignal])
def test_fetch_failure_returns_no_intent(cls):
    s = cls(_FailingClient(), _cfg())
    assert s.detect() is None


def test_strategy_names_are_distinct():
    names = {OclSignal.name, OclSellFailTopSignal.name, OclSellFailBottomSignal.name}
    assert names == {"ocl", "ocl_sell_fail_top", "ocl_sell_fail_bottom"}
