"""NSE sector/index levels: weekly parse, persist, revise, and relative strength.

Sector RS had no data path before this: NSE's allIndices is a snapshot, not a
series, so the weekly store is what makes a trend computable at all. These tests
pin the parts that would silently produce a wrong ranking.
"""
from datetime import date, timedelta

import pytest

from quant.data import nse_sectors as ns
from quant.data import sector_rs as sr

PAYLOAD = {"data": [
    {"index": "NIFTY 50", "last": "23,456.70", "open": "23,300.00",
     "high": "23,500.00", "low": "23,250.00", "percentChange": "0.45"},
    {"index": "NIFTY IT", "last": "35,100.25", "percentChange": "-0.80"},
    {"index": "BROKEN", "last": "", "percentChange": "1.0"},
]}


@pytest.fixture()
def store(tmp_path):
    return ns.SectorStore(tmp_path / "sectors.sqlite")


# --------------------------------------------------------------------------- #
# parse
# --------------------------------------------------------------------------- #
def test_parse_strips_separators_and_uppercases():
    rows = ns.parse_all_indices(PAYLOAD)
    assert rows[0]["index_name"] == "NIFTY 50"
    assert rows[0]["last"] == pytest.approx(23456.70)
    assert rows[0]["change_pct"] == pytest.approx(0.45)


def test_parse_skips_an_index_without_a_level():
    """A missing level must not become 0.0, which would rank it worst forever."""
    names = [r["index_name"] for r in ns.parse_all_indices(PAYLOAD)]
    assert "BROKEN" not in names


def test_parse_accepts_a_bare_list():
    assert len(ns.parse_all_indices(PAYLOAD["data"])) == 2


def test_parse_garbage_returns_empty():
    assert ns.parse_all_indices("nope") == []
    assert ns.parse_all_indices(None) == []


def test_parse_tags_the_supplied_date():
    rows = ns.parse_all_indices(PAYLOAD, trade_date=date(2026, 11, 14))
    assert rows[0]["trade_date"] == "2026-11-14"


# --------------------------------------------------------------------------- #
# store
# --------------------------------------------------------------------------- #
def test_save_and_read_levels(store):
    counts = store.save(ns.parse_all_indices(PAYLOAD, trade_date=date(2026, 11, 14)))
    assert counts["inserted"] == 2
    got = store.levels("2026-11-14")
    assert len(got) == 2
    assert {r["index_name"] for r in got} == {"NIFTY 50", "NIFTY IT"}


def test_resaving_the_same_week_is_idempotent(store):
    rows = ns.parse_all_indices(PAYLOAD, trade_date=date(2026, 11, 14))
    store.save(rows)
    assert store.save(rows) == {"inserted": 0, "unchanged": 2, "revised": 0}


def test_changed_level_is_recorded_not_overwritten(store):
    rows = ns.parse_all_indices(PAYLOAD, trade_date=date(2026, 11, 14))
    store.save(rows)
    changed = [{**r, "last": 1.0} for r in rows]
    counts = store.save(changed)
    assert counts["revised"] == 2
    assert len(store.revisions()) == 2
    # The ORIGINAL stands: history that silently changes cannot be audited.
    assert store.levels("2026-11-14")[0]["last"] != 1.0


def test_levels_of_an_empty_store_is_empty(store):
    assert store.levels() == []


def test_levels_default_to_the_newest_date(store):
    store.save([{"trade_date": "2026-11-07", "index_name": "NIFTY 50", "last": 1.0}])
    store.save([{"trade_date": "2026-11-14", "index_name": "NIFTY 50", "last": 2.0}])
    assert store.levels()[0]["last"] == 2.0


# --------------------------------------------------------------------------- #
# weekly series + readiness
# --------------------------------------------------------------------------- #
def _seed(store, weeks=5, base=date(2026, 10, 3)):
    for w in range(weeks):
        day = (base + timedelta(weeks=w)).isoformat()
        store.save([
            {"trade_date": day, "index_name": "NIFTY 50", "last": 23000.0 + w * 200},
            {"trade_date": day, "index_name": "NIFTY IT", "last": 35000.0 + w * 1000},
            {"trade_date": day, "index_name": "NIFTY BANK", "last": 48000.0 - w * 500},
        ])


def test_weekly_series_is_oldest_first(store):
    _seed(store)
    series = store.weekly_series("NIFTY 50")
    assert len(series) == 5
    assert series[0]["trade_date"] < series[-1]["trade_date"]


def test_insufficient_history_is_reported(store):
    _seed(store, weeks=2)
    out = ns.weekly_series("NIFTY 50", path=store.path)
    assert out["ready"] is False
    assert "insufficient_history" in out["detail"]


def test_enough_weeks_is_ready(store):
    _seed(store)
    assert ns.weekly_series("NIFTY 50", path=store.path)["ready"] is True


# --------------------------------------------------------------------------- #
# relative strength from the store
# --------------------------------------------------------------------------- #
def test_no_data_is_reported_not_faked(tmp_path):
    out = sr.sector_rs_from_store(path=tmp_path / "empty.sqlite")
    assert out["status"] == "no_data"
    assert out["sectors"] == []


def test_rs_ranks_the_outperformer_first(store):
    _seed(store)
    out = sr.sector_rs_from_store(weeks=4, path=store.path)
    assert out["status"] == "ok"
    assert out["sectors"][0]["sector"] == "NIFTY IT"      # rising fastest
    assert out["sectors"][-1]["sector"] == "NIFTY BANK"   # falling
    assert out["sectors"][0]["rank"] == 1


def test_rs_relative_is_return_minus_benchmark(store):
    _seed(store)
    out = sr.sector_rs_from_store(weeks=4, path=store.path)
    for row in out["sectors"]:
        assert row["relative"] == pytest.approx(row["return"] - _bench_return(store, 4), abs=1e-6)


def _bench_return(store, weeks):
    s = store.weekly_series("NIFTY 50")
    return s[-1]["last"] / s[-(weeks + 1)]["last"] - 1.0


def test_rs_without_enough_weeks_is_insufficient(store):
    _seed(store, weeks=2)
    out = sr.sector_rs_from_store(weeks=4, path=store.path)
    assert out["status"] == "insufficient_history"


# --------------------------------------------------------------------------- #
# fetch (offline: injected transport)
# --------------------------------------------------------------------------- #
class _Resp:
    def __init__(self, payload, status_code=200):
        self._payload, self.status_code = payload, status_code

    def json(self):
        return self._payload


def test_fetch_parses_indices():
    out = ns.fetch_sector_levels(http_get=lambda *a, **k: _Resp(PAYLOAD))
    assert out["ok"] is True and len(out["rows"]) == 2


def test_fetch_reports_forbidden_plainly():
    out = ns.fetch_sector_levels(http_get=lambda *a, **k: _Resp({}, 403))
    assert out["ok"] is False and "403" in out["detail"]


def test_fetch_survives_a_network_error():
    def boom(*a, **k):
        raise ConnectionError("down")
    assert ns.fetch_sector_levels(http_get=boom)["ok"] is False


def test_fetch_never_raises():
    for bad in ({}, [], "", None):
        out = ns.fetch_sector_levels(http_get=lambda *a, **k: _Resp(bad))
        assert isinstance(out, dict) and "ok" in out
