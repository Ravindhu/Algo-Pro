"""NSE FII/DII flow data: parse, persist, revise, staleness, fail-closed.

The stage audit found India flow data was documented as implemented but had no
data path. These tests pin the behaviour that makes it real: a bad payload is
never guessed at, a missing day is never 0, and a revised figure never silently
rewrites history.
"""
import os
import tempfile
from datetime import date, timedelta

import pytest

from quant.data import nse_fii as f

PAYLOAD = [
    {"category": "FII/FPI *", "date": "14-Nov-2026",
     "buyValue": "12,345.67", "sellValue": "10,000.00", "netValue": "2,345.67"},
    {"category": "DII **", "date": "14-Nov-2026",
     "buyValue": "8,000.00", "sellValue": "9,500.50", "netValue": "-1,500.50"},
]


@pytest.fixture()
def store(tmp_path):
    return f.FiiStore(tmp_path / "fii.sqlite")


# --------------------------------------------------------------------------- #
# parsing
# --------------------------------------------------------------------------- #
def test_parse_normalises_categories_and_strips_separators():
    rows = f.parse_fii_dii(PAYLOAD)
    assert [r["category"] for r in rows] == ["FII", "DII"]
    fii = rows[0]
    assert fii["trade_date"] == "2026-11-14"
    assert fii["net_value"] == pytest.approx(2345.67)
    assert fii["buy_value"] == pytest.approx(12345.67)


def test_parse_handles_negative_dii_value():
    assert f.parse_fii_dii(PAYLOAD)[1]["net_value"] == pytest.approx(-1500.50)


def test_parse_skips_unknown_category():
    rows = f.parse_fii_dii([{"category": "CLIENT", "date": "14-Nov-2026",
                             "netValue": "1.00"}])
    assert rows == []


def test_parse_skips_unparseable_date():
    rows = f.parse_fii_dii([{"category": "FII", "date": "not-a-date",
                             "netValue": "1.00"}])
    assert rows == []


def test_parse_uses_supplied_date_when_payload_has_none():
    rows = f.parse_fii_dii([{"category": "FII", "netValue": "5.00"}],
                           trade_date=date(2026, 11, 14))
    assert rows[0]["trade_date"] == "2026-11-14"


def test_parse_unparseable_number_is_none_not_zero():
    """A missing number must not become a real 0.00 flow."""
    rows = f.parse_fii_dii([{"category": "FII", "date": "14-Nov-2026",
                             "netValue": "n/a"}])
    assert rows[0]["net_value"] is None


def test_parse_accepts_a_wrapped_payload():
    rows = f.parse_fii_dii({"data": PAYLOAD})
    assert len(rows) == 2


def test_parse_garbage_returns_empty():
    assert f.parse_fii_dii("not a payload") == []
    assert f.parse_fii_dii(None) == []


# --------------------------------------------------------------------------- #
# storage
# --------------------------------------------------------------------------- #
def test_save_and_read_back(store):
    assert store.save(f.parse_fii_dii(PAYLOAD)) == {
        "inserted": 2, "unchanged": 0, "revised": 0}
    assert store.latest("FII")["net_value"] == pytest.approx(2345.67)


def test_resaving_the_same_day_is_idempotent(store):
    rows = f.parse_fii_dii(PAYLOAD)
    store.save(rows)
    counts = store.save(rows)
    assert counts == {"inserted": 0, "unchanged": 2, "revised": 0}
    assert len(store.history()) == 2


def test_a_revised_figure_is_recorded_not_overwritten(store):
    rows = f.parse_fii_dii(PAYLOAD)
    store.save(rows)
    changed = [{**r, "net_value": 999.0} for r in rows]
    counts = store.save(changed)
    assert counts["revised"] == 2
    # Original stands; the disagreement is kept separately.
    assert store.latest("FII")["net_value"] == pytest.approx(2345.67)
    assert len(store.revisions()) == 2


def test_latest_of_an_empty_store_is_none(store):
    assert store.latest("FII") is None


def test_history_filters_by_date(store):
    store.save(f.parse_fii_dii(PAYLOAD))
    store.save(f.parse_fii_dii([{**PAYLOAD[0], "date": "15-Nov-2026"}]))
    assert len(store.history(start="2026-11-15")) == 1
    assert len(store.history(end="2026-11-14")) == 2


# --------------------------------------------------------------------------- #
# staleness (fail closed)
# --------------------------------------------------------------------------- #
def test_missing_row_is_stale_not_fresh():
    out = f.staleness(None)
    assert out["known"] is False
    assert out["stale"] is True


def test_recent_row_is_fresh():
    row = {"trade_date": date.today().isoformat()}
    assert f.staleness(row)["stale"] is False


def test_old_row_is_stale():
    old = (date.today() - timedelta(days=10)).isoformat()
    out = f.staleness({"trade_date": old})
    assert out["stale"] is True
    assert out["age_days"] == 10


def test_unparseable_date_is_stale():
    assert f.staleness({"trade_date": "??"})["stale"] is True


# --------------------------------------------------------------------------- #
# fetch (offline: injected transport)
# --------------------------------------------------------------------------- #
class _Resp:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload


def test_fetch_parses_a_good_response():
    out = f.fetch_fii_dii(http_get=lambda *a, **k: _Resp(PAYLOAD))
    assert out["ok"] is True
    assert len(out["rows"]) == 2


def test_fetch_reports_http_403_plainly():
    out = f.fetch_fii_dii(http_get=lambda *a, **k: _Resp({}, status_code=403))
    assert out["ok"] is False
    assert "403" in out["detail"]


def test_fetch_survives_a_network_exception():
    def boom(*a, **k):
        raise ConnectionError("no route")
    out = f.fetch_fii_dii(http_get=boom)
    assert out["ok"] is False
    assert "request failed" in out["detail"]


def test_fetch_reports_empty_payload():
    out = f.fetch_fii_dii(http_get=lambda *a, **k: _Resp([]))
    assert out["ok"] is False


def test_fetch_never_raises():
    """A scheduled job must get a result, not a traceback."""
    for bad in ({}, [], "", None):
        out = f.fetch_fii_dii(http_get=lambda *a, **k: _Resp(bad))
        assert isinstance(out, dict) and "ok" in out


# --------------------------------------------------------------------------- #
# market_inputs integration
# --------------------------------------------------------------------------- #
def test_market_inputs_reports_missing_data_as_unknown(monkeypatch, tmp_path):
    monkeypatch.setenv("QUANT_FII_DB", str(tmp_path / "none.sqlite"))
    from quant.data import market_inputs as mi
    out = mi.fii_flows()
    assert out["known"] is False
    assert out["net_value"] is None
    assert out["stale"] is True


def test_market_inputs_serves_a_stored_value(monkeypatch, tmp_path):
    monkeypatch.setenv("QUANT_FII_DB", str(tmp_path / "has.sqlite"))
    from quant.data import market_inputs as mi
    f.save_flows(f.parse_fii_dii(PAYLOAD))
    out = mi.fii_flows("FII")
    assert out["known"] is True
    assert out["net_value"] == pytest.approx(2345.67)
    assert out["stale"] is False
