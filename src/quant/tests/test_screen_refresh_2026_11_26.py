"""Auto screen refresh (2026-11-26): the bot loop refreshes every 5 minutes; no cache of surveillance flags.

Offline: fake quotes and a fake clock. No broker calls.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from types import SimpleNamespace

from quant.data import screen_refresh as sr
from quant.data.screen_refresh import load_candidates, make_screen_refresher, refresh_once
from scripts.run import run_strategy_bot as runner

NOW = datetime(2026, 11, 26, 10, 0)


def _quote(symbol, asm=False, upper=110.0, lower=90.0):
    return {"status": "success", "data": {"upper_circuit_limit": upper, "lower_circuit_limit": lower,
                                          "prev_close": 100.0, "asm": asm, "gsm": False}}


class _Audit:
    def __init__(self):
        self.rows = []

    def append(self, kind, payload):
        self.rows.append((kind, payload))
        return kind


class _Bot:
    def __init__(self):
        self.cfg = SimpleNamespace(interval_minutes=5, vwap_poll_seconds=15)
        self.cycles = 0

    def cycle(self, now):
        self.cycles += 1


class _Clock:
    """Advances 60 s per call, so refresh timing is deterministic."""

    def __init__(self):
        self.n = 0

    def __call__(self):
        self.n += 1
        return NOW + timedelta(seconds=60 * self.n)


# ---- the loop --------------------------------------------------------------------------------

def test_refresh_runs_at_start_and_then_about_every_five_minutes(tmp_path):
    bot, audit, clock = _Bot(), _Audit(), _Clock()
    calls = []

    def refresh(now):
        calls.append(now)
        return {"exit_code": 0}

    runner.run(bot, audit, state_dir=tmp_path, clock=clock, sleep=lambda s: None,
               max_cycles=12, refresh=refresh, refresh_every_seconds=300)
    assert len(calls) >= 2
    assert len(calls) < bot.cycles          # not on every cycle
    gaps = [(b - a).total_seconds() for a, b in zip(calls, calls[1:])]
    assert all(g >= 300 for g in gaps)


def test_a_failed_refresh_is_audited_and_the_bot_keeps_cycling(tmp_path):
    bot, audit, clock = _Bot(), _Audit(), _Clock()

    def refresh(now):
        raise RuntimeError("broker down")

    runner.run(bot, audit, state_dir=tmp_path, clock=clock, sleep=lambda s: None,
               max_cycles=3, refresh=refresh)
    assert bot.cycles == 3
    assert any(k == "screen_refresh_error" for k, _ in audit.rows)


def test_no_refresh_hook_leaves_the_loop_as_before(tmp_path):
    bot, audit, clock = _Bot(), _Audit(), _Clock()
    runner.run(bot, audit, state_dir=tmp_path, clock=clock, sleep=lambda s: None, max_cycles=2)
    assert bot.cycles == 2
    assert not any(k.startswith("screen_refresh") for k, _ in audit.rows)


# ---- one refresh ------------------------------------------------------------------------------

def test_refresh_once_writes_only_the_tradable_symbols(tmp_path):
    target = tmp_path / "latest.json"
    candidates = [{"symbol": "AAA", "selected": True}, {"symbol": "BBB", "selected": True},
                  {"symbol": "CCC", "selected": True}]

    def quote(symbol, exchange):
        if symbol == "CCC":
            raise RuntimeError("timeout")
        return _quote(symbol, asm=(symbol == "BBB"))

    out = refresh_once(candidates, quote, target)
    doc = json.loads(target.read_text(encoding="utf-8"))
    kept = [r["symbol"] for r in doc["results"]]
    assert kept == ["AAA"]                  # BBB flagged, CCC unreachable: both dropped
    assert out["wrote"] == 1 and out["exit_code"] == 0


def test_surveillance_flag_is_not_cached_between_refreshes(tmp_path):
    target = tmp_path / "latest.json"
    candidates = [{"symbol": "AAA", "selected": True}]
    flag = {"asm": False}

    def quote(symbol, exchange):
        return _quote(symbol, asm=flag["asm"])

    first = refresh_once(candidates, quote, target)
    assert first["wrote"] == 1
    flag["asm"] = True                      # the exchange puts AAA under surveillance mid-day
    second = refresh_once(candidates, quote, target)
    assert second["wrote"] == 0             # the next refresh sees it; nothing stale is reused


def test_every_quote_failing_is_exit_code_1(tmp_path):
    def quote(symbol, exchange):
        raise RuntimeError("session expired")

    out = refresh_once([{"symbol": "AAA", "selected": True}], quote, tmp_path / "latest.json")
    assert out["exit_code"] == 1


def test_enrichment_runs_in_parallel_and_keeps_order():
    import threading
    seen = set()
    lock = threading.Lock()

    def quote(symbol, exchange):
        with lock:
            seen.add(threading.get_ident())
        return _quote(symbol)

    rows = [{"symbol": f"S{i}"} for i in range(12)]
    out = sr.enrich_parallel(rows, quote, "NSE", workers=4)
    assert [r["symbol"] for r in out] == [f"S{i}" for i in range(12)]
    assert len(seen) > 1                    # more than one worker thread


# ---- candidates and the refresher ---------------------------------------------------------------

def test_missing_source_gives_no_candidates_and_exit_2(tmp_path):
    assert load_candidates(tmp_path / "missing.json") == []
    refresher = make_screen_refresher(object(), "KEY", source=tmp_path / "missing.json",
                                      target=tmp_path / "latest.json")
    assert refresher(NOW)["exit_code"] == 2


def test_refresher_reads_selected_rows_from_the_saved_screen(tmp_path):
    src = tmp_path / "last.json"
    src.write_text(json.dumps({"results": [
        {"symbol": "AAA", "selected": True}, {"symbol": "ZZZ", "selected": False}]}), encoding="utf-8")

    class Reader:
        def quotes(self, key, symbol, exchange):
            return _quote(symbol)

    target = tmp_path / "latest.json"
    out = make_screen_refresher(Reader(), "KEY", source=src, target=target)(NOW)
    assert out["candidates"] == 1 and out["wrote"] == 1
    assert [r["symbol"] for r in json.loads(target.read_text())["results"]] == ["AAA"]
