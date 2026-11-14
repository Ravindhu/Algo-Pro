"""
test_optimizer_data_cache.py - the new data cache, in isolation.

This is the test file for Phase 1 of Temp/OPTIMIZER_HEADER_PLAN.md. It
covers the new ``blue_engine.optimizer.data_cache`` module: a thin
wrapper around ``client.history()`` + the existing ``PriceCache`` SQLite
store.

The tests live in their own file so a stale import in the pre-existing
``test_optimizer_engine.py`` (it references a private constant
``SYNTHETIC_DEMO`` that is named ``_SYNTHETIC_DEMO`` in the source) does
not block the new tests from running.

RUNNING
-------
    & ..\\..\\.venv\\Scripts\\python.exe -m pytest tests/test_optimizer_data_cache.py -v
"""
from __future__ import annotations

import os
import sys
import json

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import pytest  # noqa: E402

from blue_engine.optimizer.data_cache import (  # noqa: E402
    MAX_WINDOW_DAYS, OptimizerDataCache,
    DownloadResult, LoadResult, StatusResult,
    list_strategies_for_ui, live_active_state, watchlist_for_ui,
)


def _check(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)


def _tmp_data_cache(tmp_path) -> OptimizerDataCache:
    root = tmp_path / "opt_cache"
    return OptimizerDataCache(root=root)


class _StubClient:
    """Minimal stand-in for the OpenAlgo SDK client."""

    def __init__(self, *, history_payload=None, instruments_payload=None):
        self._history_payload = history_payload
        self._instruments_payload = instruments_payload
        self.history_calls: list = []
        self.instruments_calls: list = []

    def history(self, **kwargs):
        self.history_calls.append(kwargs)
        return self._history_payload

    def instruments(self, **kwargs):
        self.instruments_calls.append(kwargs)
        return self._instruments_payload


# --------------------------------------------------------------------------- #
# 1. download() is honest on no-client
# --------------------------------------------------------------------------- #


def test_data_cache_download_returns_ok_false_when_no_client(monkeypatch, tmp_path):
    """A download with no SDK client must NOT raise; it must return
    ``ok=False`` with a reason. The dashboard renders this as a "broker
    offline" panel; raising would crash the page.
    """
    from blue_engine.portfolio.providers import factory as factory_mod
    monkeypatch.setattr(factory_mod, "get_client", lambda: None)
    cache = _tmp_data_cache(tmp_path)
    res = cache.download(exchange="NSE", symbol="RELIANCE",
                          interval="1d", start="2025-01-01", end="2025-02-01")
    _check(res.ok is False, f"download must return ok=False; got {res}")
    _check(res.reason == "no_client",
           f"reason must be 'no_client' for an unset SDK; got {res.reason!r}")
    _check("no OPENALGO_API_KEY" in (res.warnings[0] if res.warnings else ""),
           f"warning must mention OPENALGO_API_KEY; got {res.warnings}")


# --------------------------------------------------------------------------- #
# 2. download() is honest on bad dates
# --------------------------------------------------------------------------- #


def test_data_cache_download_returns_ok_false_on_bad_dates(monkeypatch, tmp_path):
    from blue_engine.portfolio.providers import factory as factory_mod
    monkeypatch.setattr(factory_mod, "get_client", lambda: _StubClient())
    cache = _tmp_data_cache(tmp_path)
    res = cache.download(exchange="NSE", symbol="RELIANCE",
                          interval="1d", start="not-a-date", end="2025-02-01")
    _check(res.ok is False, f"bad start must give ok=False; got {res}")
    _check(res.reason == "bad_date_format",
           f"reason must be 'bad_date_format'; got {res.reason!r}")


def test_data_cache_download_returns_ok_false_on_end_before_start(monkeypatch, tmp_path):
    from blue_engine.portfolio.providers import factory as factory_mod
    monkeypatch.setattr(factory_mod, "get_client", lambda: _StubClient())
    cache = _tmp_data_cache(tmp_path)
    res = cache.download(exchange="NSE", symbol="RELIANCE",
                          interval="1d", start="2025-02-01", end="2025-01-01")
    _check(res.ok is False, f"end<start must give ok=False; got {res}")
    _check(res.reason == "end_before_start",
           f"reason must be 'end_before_start'; got {res.reason!r}")


# --------------------------------------------------------------------------- #
# 3. download() honours the 30-day cap by pruning and warning
# --------------------------------------------------------------------------- #


def test_data_cache_download_prunes_windows_larger_than_max(monkeypatch, tmp_path):
    from blue_engine.portfolio.providers import factory as factory_mod
    from datetime import datetime

    client = _StubClient(history_payload={
        "status": "success",
        "data": [
            {"timestamp": "2024-12-01T00:00:00", "open": 100, "high": 101,
             "low": 99, "close": 100.5, "volume": 1000},
            {"timestamp": "2024-12-02T00:00:00", "open": 100, "high": 101,
             "low": 99, "close": 101.0, "volume": 1100},
        ],
    })
    monkeypatch.setattr(factory_mod, "get_client", lambda: client)
    cache = _tmp_data_cache(tmp_path)
    res = cache.download(
        exchange="NSE", symbol="RELIANCE", interval="1d",
        start="2020-01-01", end="2025-01-01")
    _check(res.ok, f"download must succeed when the SDK returns data; got {res}")
    _check(any("exceeds" in w and "hard prune" in w for w in res.warnings),
           f"warnings must mention the hard prune; got {res.warnings}")
    _check(len(client.history_calls) == 1,
           f"history must be called exactly once; got {len(client.history_calls)}")
    call = client.history_calls[0]
    end_dt = datetime.strptime(call["end_date"], "%Y-%m-%d")
    start_dt = datetime.strptime(call["start_date"], "%Y-%m-%d")
    _check((end_dt - start_dt).days <= MAX_WINDOW_DAYS,
           f"history window must be pruned to <= {MAX_WINDOW_DAYS}d; "
           f"got {(end_dt - start_dt).days}")


# --------------------------------------------------------------------------- #
# 4. download() is idempotent
# --------------------------------------------------------------------------- #


def test_data_cache_download_is_idempotent(monkeypatch, tmp_path):
    """A second download of the same window must NOT duplicate rows in the
    SQLite cache. The cache's contract is
    "re-fetching an overlapping window updates in place rather than
    duplicating, so the cache can be safely refreshed daily."

    The ``upsert`` helper returns the number of rows affected (inserts
    AND updates); the proof of idempotency is that the cache still holds
    exactly one row, not two.
    """
    from blue_engine.portfolio.providers import factory as factory_mod

    payload = {
        "status": "success",
        "data": [
            {"timestamp": "2024-12-01T00:00:00", "open": 100, "high": 101,
             "low": 99, "close": 100.5, "volume": 1000},
        ],
    }
    monkeypatch.setattr(factory_mod, "get_client",
                         lambda: _StubClient(history_payload=payload))
    cache = _tmp_data_cache(tmp_path)
    first = cache.download(exchange="NSE", symbol="RELIANCE", interval="1d",
                            start="2024-12-01", end="2024-12-02")
    _check(first.ok and first.n_written == 1,
           f"first write must succeed; got {first}")
    second = cache.download(exchange="NSE", symbol="RELIANCE", interval="1d",
                             start="2024-12-01", end="2024-12-02")
    _check(second.ok,
           f"second write must succeed; got {second}")
    # The proof of idempotency is the row count, not the n_written counter
    # (upsert counts inserts AND updates as affected rows).
    _check(cache._count_rows("RELIANCE", "NSE", "1d") == 1,
           f"cache must still hold exactly one row; got {cache._count_rows('RELIANCE','NSE','1d')}")


# --------------------------------------------------------------------------- #
# 5. load() prefers the cache; falls back to download when empty
# --------------------------------------------------------------------------- #


def test_data_cache_load_uses_cache_when_present(monkeypatch, tmp_path):
    from blue_engine.portfolio.providers import factory as factory_mod

    payload = {
        "status": "success",
        "data": [
            {"timestamp": "2024-12-01T00:00:00", "open": 100, "high": 101,
             "low": 99, "close": 100.5, "volume": 1000},
        ],
    }
    client = _StubClient(history_payload=payload)
    monkeypatch.setattr(factory_mod, "get_client", lambda: client)
    cache = _tmp_data_cache(tmp_path)
    cache.download(exchange="NSE", symbol="RELIANCE", interval="1d",
                    start="2024-12-01", end="2024-12-02")
    client.history_calls = []
    res = cache.load(exchange="NSE", symbol="RELIANCE", interval="1d",
                      start="2024-12-01", end="2024-12-02")
    _check(res.ok, f"load must succeed from the cache; got {res}")
    _check(res.source == "cache",
           f"load from cache must report source='cache'; got {res.source!r}")
    _check(res.n_bars == 1, f"load must return 1 bar; got {res.n_bars}")
    _check(client.history_calls == [],
           f"load must NOT call history() when the cache has the bars; "
           f"got calls={client.history_calls}")


def test_data_cache_load_falls_back_to_download_when_empty(monkeypatch, tmp_path):
    from blue_engine.portfolio.providers import factory as factory_mod

    payload = {
        "status": "success",
        "data": [
            {"timestamp": "2024-12-01T00:00:00", "open": 100, "high": 101,
             "low": 99, "close": 100.5, "volume": 1000},
        ],
    }
    monkeypatch.setattr(factory_mod, "get_client",
                         lambda: _StubClient(history_payload=payload))
    cache = _tmp_data_cache(tmp_path)
    res = cache.load(exchange="NSE", symbol="RELIANCE", interval="1d",
                      start="2024-12-01", end="2024-12-02")
    _check(res.ok, f"load must succeed via download; got {res}")
    _check(res.source == "downloaded",
           f"load after download must report source='downloaded'; got {res.source!r}")
    _check(res.n_bars == 1, f"load must return 1 bar; got {res.n_bars}")


def test_data_cache_load_returns_ok_false_when_no_data(monkeypatch, tmp_path):
    from blue_engine.portfolio.providers import factory as factory_mod
    monkeypatch.setattr(factory_mod, "get_client",
                         lambda: _StubClient(history_payload={"status": "success", "data": []}))
    cache = _tmp_data_cache(tmp_path)
    res = cache.load(exchange="NSE", symbol="RELIANCE", interval="1d",
                      start="2024-12-01", end="2024-12-02")
    _check(res.ok is False, f"empty SDK response must give ok=False; got {res}")
    _check(res.bars is None, f"bars must be None on failure; got {res.bars}")


# --------------------------------------------------------------------------- #
# 6. status() reports the cache's view of one instrument
# --------------------------------------------------------------------------- #


def test_data_cache_status_reports_cached_instrument(monkeypatch, tmp_path):
    from blue_engine.portfolio.providers import factory as factory_mod

    payload = {
        "status": "success",
        "data": [
            {"timestamp": "2024-12-01T00:00:00", "open": 100, "high": 101,
             "low": 99, "close": 100.5, "volume": 1000},
            {"timestamp": "2024-12-02T00:00:00", "open": 100, "high": 101,
             "low": 99, "close": 101.0, "volume": 1100},
        ],
    }
    monkeypatch.setattr(factory_mod, "get_client",
                         lambda: _StubClient(history_payload=payload))
    cache = _tmp_data_cache(tmp_path)
    cache.download(exchange="NSE", symbol="RELIANCE", interval="1d",
                    start="2024-12-01", end="2024-12-03")
    s = cache.status(exchange="NSE", symbol="RELIANCE", interval="1d")
    _check(s.cached is True, f"primed cache must report cached=True; got {s}")
    _check(s.n_bars == 2, f"status must report n_bars=2; got {s.n_bars}")
    _check(s.first_ts and s.last_ts,
           f"status must report first/last ts; got {s}")
    empty = cache.status(exchange="NSE", symbol="TCS", interval="1d")
    _check(empty.cached is False, f"empty cache must report cached=False; got {empty}")
    _check(empty.source == "missing",
           f"empty cache must report source='missing'; got {empty.source!r}")


# --------------------------------------------------------------------------- #
# 7. cached_keys() returns the unique tuples
# --------------------------------------------------------------------------- #


def test_data_cache_cached_keys_lists_every_instrument(tmp_path):
    cache = _tmp_data_cache(tmp_path)
    _check(cache.cached_keys() == [],
           f"empty cache must return []; got {cache.cached_keys()}")
    from blue_engine.portfolio.providers import price_cache as pc
    pc.PriceCache(db_path=cache.db_path).upsert(
        "RELIANCE", "NSE", "1d",
        [{"ts": "2024-12-01", "open": 100, "high": 101, "low": 99,
          "close": 100.5, "volume": 1000}])
    pc.PriceCache(db_path=cache.db_path).upsert(
        "TCS", "NSE", "1d",
        [{"ts": "2024-12-01", "open": 200, "high": 201, "low": 199,
          "close": 200.5, "volume": 2000}])
    keys = cache.cached_keys()
    syms = sorted(k["symbol"] for k in keys)
    _check(syms == ["RELIANCE", "TCS"],
           f"cached_keys must list every symbol; got {syms}")


# --------------------------------------------------------------------------- #
# 8. watchlist_for_ui() returns a list and never raises
# --------------------------------------------------------------------------- #


def test_watchlist_for_ui_returns_a_list():
    out = watchlist_for_ui(preset=None, extra_symbols=None)
    _check(isinstance(out, list),
           f"watchlist_for_ui must return a list; got {type(out).__name__}")


def test_watchlist_for_ui_accepts_extra_symbols():
    extras = [{"exchange": "NSE", "symbol": "RELIANCE", "name": "Reliance"}]
    out = watchlist_for_ui(preset=None, extra_symbols=extras, exchange="NSE")
    if out:
        names = {e.get("symbol") for e in out}
        _check("RELIANCE" in names or not out,
               f"RELIANCE must appear when extras carry it; got {out}")


# --------------------------------------------------------------------------- #
# 9. list_strategies_for_ui() returns a list of dicts
# --------------------------------------------------------------------------- #


def test_list_strategies_for_ui_returns_a_list_of_dicts():
    out = list_strategies_for_ui()
    _check(isinstance(out, list),
           f"list_strategies_for_ui must return a list; got {type(out).__name__}")
    for entry in out:
        _check(isinstance(entry, dict),
               f"each entry must be a dict; got {type(entry).__name__}")
        for key in ("name", "label", "module", "class_name"):
            _check(key in entry,
                   f"each entry must carry {key!r}; got keys {list(entry.keys())}")


def test_list_strategies_for_ui_picks_up_ocl_keylevel():
    """The OCL keylevel signal lives at
    ``blue_engine/alpha/Statergy/ocl_keylevel.py``. The dynamic walker
    in ``alpha.signals._register_strategies`` scans that folder at
    import time, so the OCL signal must appear in the dropdown
    payload with name ``ocl_keylevel``.

    This is the test that fails if the walker is broken or if the OCL
    file's relative imports cannot resolve in its current location.
    """
    out = list_strategies_for_ui()
    names = {e["name"] for e in out}
    _check("ocl_keylevel" in names,
           f"the OCL keylevel strategy must be in the dropdown; got {names}")
    ocl = next(e for e in out if e["name"] == "ocl_keylevel")
    _check(ocl["module"] == "blue_engine.alpha.Statergy.ocl_keylevel",
           f"module path must point at alpha.Statergy; got {ocl['module']!r}")
    _check(ocl["class_name"] == "OCLKeylevelSignal",
           f"class_name must be OCLKeylevelSignal; got {ocl['class_name']!r}")


def test_list_strategies_for_ui_includes_params_space_for_ocl():
    """The OCL keylevel signal carries a ``params_space`` class
    attribute; the dropdown payload must surface it so the AI Insight
    header can render the parameter table.
    """
    out = list_strategies_for_ui()
    ocl = next((e for e in out if e["name"] == "ocl_keylevel"), None)
    _check(ocl is not None,
           "OCL keylevel must be in the dropdown payload")
    params = ocl.get("params_space") or {}
    _check(isinstance(params, dict) and len(params) > 0,
           f"params_space must be a non-empty dict; got {params!r}")
    # Sanity: a few well-known OCL parameters must be present.
    # NOTE (audit F-24): ``min_body_pct`` is deliberately ABSENT. The body-size
    # filter was removed from ``alpha/signals/_pattern.py`` by operator
    # instruction, so the strategy accepts-and-ignores the value. Exposing it as
    # a tunable parameter (or requiring it in the UI payload) would advertise a
    # control that changes nothing and would burn optimizer trials on a flat axis.
    for p in ("htf_interval", "pattern_type", "exec_mode"):
        _check(p in params,
               f"params_space must include {p!r}; got keys {sorted(params.keys())}")
    _check("min_body_pct" not in params,
           "min_body_pct must NOT be tunable: the strategy ignores it (F-24)")


def test_list_strategies_for_ui_is_dynamic_per_drop_in():
    """The walker is dynamic: ``blue_engine.alpha.signals`` exposes
    ``_register_strategies`` which scans the Statergy folder at
    import time. A new file dropped into that folder appears in the
    dropdown with no code change. This test pins the walker as
    importable and callable so a future refactor that renames it
    breaks here, not in the dashboard.
    """
    from blue_engine.alpha import signals as _signals
    _check(hasattr(_signals, "_register_strategies"),
           "alpha.signals must expose _register_strategies")
    _check(callable(_signals._register_strategies),
           "_register_strategies must be callable")


# --------------------------------------------------------------------------- #
# 10. live_active_state() reads the live runner's config
# --------------------------------------------------------------------------- #


def test_live_active_state_returns_none_when_file_missing(tmp_path):
    res = live_active_state(path=tmp_path / "does_not_exist.json")
    _check(res is None,
           f"missing file must return None; got {res!r}")


def test_live_active_state_returns_dict_when_file_present(tmp_path):
    target = tmp_path / "strategy.active.json"
    target.write_text(json.dumps({
        "strategy": "ocl_keylevel",
        "module": "blue_engine.alpha.Statergy.ocl_keylevel",
        "symbol": "RELIANCE", "exchange": "NSE", "interval": "5minute",
        "started_at": "2025-10-01T09:15:00+05:30",
        "params": {"htf_interval": "15minute", "min_body_pct": 0.3},
    }), encoding="utf-8")
    res = live_active_state(path=target)
    _check(isinstance(res, dict),
           f"present file must return a dict; got {type(res).__name__}")
    _check(res.get("strategy") == "ocl_keylevel",
           f"strategy key must round-trip; got {res.get('strategy')!r}")
    _check(res.get("symbol") == "RELIANCE",
           f"symbol key must round-trip; got {res.get('symbol')!r}")


def test_live_active_state_returns_dict_with_error_key_on_bad_json(tmp_path):
    target = tmp_path / "strategy.active.json"
    target.write_text("{ this is not json", encoding="utf-8")
    res = live_active_state(path=target)
    _check(isinstance(res, dict) and "error" in res,
           f"bad JSON must return dict with 'error'; got {res!r}")


# --------------------------------------------------------------------------- #
# Bonus: result types carry the documented fields
# --------------------------------------------------------------------------- #


def test_data_cache_result_types_have_documented_fields():
    from dataclasses import fields as dc_fields
    for cls, fields_ in [
        (DownloadResult, ("ok", "symbol", "exchange", "interval",
                          "n_written", "first_ts", "last_ts",
                          "reason", "warnings")),
        (LoadResult, ("ok", "symbol", "exchange", "interval", "start", "end",
                       "n_bars", "first_ts", "last_ts", "source",
                       "data_class", "reason", "warnings", "bars")),
        (StatusResult, ("cached", "symbol", "exchange", "interval",
                         "n_bars", "first_ts", "last_ts", "source")),
    ]:
        actual = {f.name for f in dc_fields(cls)}
        for f in fields_:
            _check(f in actual,
                   f"{cls.__name__} must carry {f!r}; got {sorted(actual)}")
