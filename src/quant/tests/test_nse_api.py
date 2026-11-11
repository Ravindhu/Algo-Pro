"""NSE data layer: offline tests with an injected fetch. No network in the suite."""
import pytest

from quant.data.nse_api import NseApi, NseUnavailable, _rows, security_beta


def test_rows_normalises_list_and_dataframe():
    assert _rows([{"a": 1}, "junk"]) == [{"a": 1}]
    assert _rows({"not": "a list"}) == []

    class DF:
        def to_dict(self, orient):
            assert orient == "records"
            return [{"b": 2}]

    assert _rows(DF()) == [{"b": 2}]


def test_api_uses_injected_fetch_and_normalises():
    calls = []

    def fetch(name, args, kwargs):
        calls.append((name, args))
        if name == "nse_holidays":
            return [{"tradingDate": "26-Jan-2026", "description": "Republic Day"}]
        if name == "nse_blockdeal":
            return [{"symbol": "INFY", "qty": 1000}]
        if name == "get_bulkdeals":
            return [{"symbol": "TCS"}]
        if name == "nse_events":
            return [{"symbol": "INFY", "purpose": "Results"}]
        if name == "nse_fiidii":
            return {"FII": -100.0}
        if name == "nse_get_index_quote":
            return {"last": "24,000.00"}
        return None

    api = NseApi(fetch)
    assert api.holidays()[0]["description"] == "Republic Day"
    assert api.block_deals()[0]["symbol"] == "INFY"
    assert api.bulk_deals()[0]["symbol"] == "TCS"
    assert api.events()[0]["purpose"] == "Results"
    assert api.fii_dii() == {"FII": -100.0}
    assert api.index_quote("NIFTY 50")["last"] == "24,000.00"
    assert ("nse_holidays", ("trading",)) in calls


def test_api_returns_empty_on_bad_payload():
    api = NseApi(lambda name, args, kwargs: "not a list")
    assert api.holidays() == []
    assert api.block_deals() == []


def test_missing_function_raises(monkeypatch):
    import quant.data.nse_api as mod

    class Fake:
        pass

    monkeypatch.setattr(mod, "_mod", lambda: Fake())
    with pytest.raises(NseUnavailable):
        mod._call("nse_holidays", "trading")


def test_security_beta_returns_none_when_unavailable(monkeypatch):
    import quant.data.nse_api as mod

    def boom(*a, **k):
        raise NseUnavailable("down")

    monkeypatch.setattr(mod, "_call", boom)
    assert security_beta("INFY") is None


def test_security_beta_reads_number(monkeypatch):
    import quant.data.nse_api as mod

    monkeypatch.setattr(mod, "_call", lambda *a, **k: 1.23)
    assert security_beta("INFY") == pytest.approx(1.23)
