from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from blue_engine.core.risk_context import fetch_risk_context  # noqa: E402


class _Broker:
    def positionbook(self):
        return {
            "status": "success",
            "data": [{"pnl": -250.0, "netqty": 10, "avgprc": 100.0}],
        }


def test_day_pnl_override_does_not_suppress_broker_exposure():
    result = fetch_risk_context(_Broker(), {"risk": {"day_pnl": -500.0}})

    assert result["day_pnl"] == -500.0
    assert result["open_exposure"] == 1_000.0
    assert result["source"] == "mixed"
    assert result["ok"] is True


def test_exposure_override_does_not_suppress_broker_day_pnl():
    result = fetch_risk_context(
        _Broker(), {"risk": {"open_exposure": 2_000.0}})

    assert result["day_pnl"] == -250.0
    assert result["open_exposure"] == 2_000.0
    assert result["source"] == "mixed"
    assert result["ok"] is True


def test_missing_broker_fallback_is_reported_without_erasing_override():
    result = fetch_risk_context(
        None, {"risk": {"day_pnl": -500.0}})

    assert result["day_pnl"] == -500.0
    assert result["open_exposure"] is None
    assert result["ok"] is False
    assert result["warnings"]
