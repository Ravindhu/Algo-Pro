"""Optional algo ID on orders (flow item 13). Off by default; on when require_algo_id is set."""
from quant.core.config import RiskLimits
from quant.core.types import TradeIntent
from quant.risk.pretrade import pretrade_checks


def _intent(algo_id=None):
    return TradeIntent("i1", "s1", "INFY", "BUY", 10, 100.0, algo_id=algo_id)


def test_default_does_not_require_tag():
    assert not any("algo ID" in r for r in pretrade_checks(_intent(), RiskLimits()))


def test_required_tag_missing_is_refused():
    reasons = pretrade_checks(_intent(), RiskLimits(), require_algo_id=True)
    assert any("algo ID missing" in r for r in reasons)


def test_required_tag_present_passes():
    reasons = pretrade_checks(_intent("ALGO-123"), RiskLimits(), require_algo_id=True)
    assert not any("algo ID" in r for r in reasons)
