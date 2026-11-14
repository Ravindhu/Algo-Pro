"""Market-session and API-key resolution tests (audit 2026-10-29).

LIVE-10: nothing loaded the repository-root ``.env``, so ``OPENALGO_API_KEY``
resolved to None and the SDK client was built unauthenticated - the run then
failed at its first broker call instead of at startup.

LIVE-11: there was no weekday/session guard anywhere. A live manual intent
placed on a Sunday or after 15:30 was sent to the broker, and a run started
before 09:15 polled a closed book.
"""
from __future__ import annotations

from datetime import datetime

import pytest

from blue_engine.core.time_utils import IST, market_session_state


def _ist(text: str) -> datetime:
    return datetime.strptime(text, "%Y-%m-%d %H:%M").replace(tzinfo=IST)


# --------------------------------------------------------------------------- #
# Session classification
# --------------------------------------------------------------------------- #
def test_before_open_is_closed():
    s = market_session_state(now=_ist("2026-11-02 09:00"))
    assert s["open"] is False
    assert s["state"] == "before_open"


def test_inside_session_is_open():
    s = market_session_state(now=_ist("2026-11-02 10:30"))
    assert s["open"] is True
    assert s["state"] == "open"


def test_after_entry_cutoff_allows_exits_only():
    s = market_session_state(now=_ist("2026-11-02 14:50"), entry_cutoff="14:45")
    assert s["open"] is True, "the market is still open for exits"
    assert s["state"] == "no_new_entries"


def test_after_close_is_closed():
    s = market_session_state(now=_ist("2026-11-02 15:30"))
    assert s["open"] is False
    assert s["state"] == "after_close"


@pytest.mark.parametrize("day", ["2026-10-31 11:00", "2026-11-01 11:00"])
def test_weekend_is_closed(day):
    s = market_session_state(now=_ist(day))
    assert s["open"] is False
    assert s["state"] == "closed_day"
    assert "Saturday" in s["reason"] or "Sunday" in s["reason"]


def test_exactly_at_open_is_open():
    assert market_session_state(now=_ist("2026-11-02 09:15"))["open"] is True


def test_state_carries_the_reference_time():
    s = market_session_state(now=_ist("2026-11-02 10:30"))
    assert s["now"].startswith("2026-11-02T10:30:00")


# --------------------------------------------------------------------------- #
# Holiday file (optional, must never claim a verified holiday when absent)
# --------------------------------------------------------------------------- #
def test_missing_holiday_file_still_allows_a_weekday(monkeypatch, tmp_path):
    import blue_engine.core.time_utils as tu
    monkeypatch.setattr(tu, "_HOLIDAY_PATH", tmp_path / "missing.json", raising=False)
    ok, why = tu.is_trading_day(_ist("2026-11-02 10:30"))
    assert ok is True
    assert "no holiday file" in why, "must not imply holidays were verified"


def test_holiday_file_blocks_a_listed_date(monkeypatch, tmp_path):
    import blue_engine.core.time_utils as tu
    path = tmp_path / "h.json"
    path.write_text('["2026-11-03"]', encoding="utf-8")
    monkeypatch.setattr(tu, "_HOLIDAY_PATH", path, raising=False)
    ok, why = tu.is_trading_day(_ist("2026-11-03 10:30"))
    assert ok is False
    assert "holiday" in why
    # A different weekday is unaffected.
    assert tu.is_trading_day(_ist("2026-11-04 10:30"))[0] is True


def test_holiday_file_accepts_an_object_form(monkeypatch, tmp_path):
    import blue_engine.core.time_utils as tu
    path = tmp_path / "h.json"
    path.write_text('{"dates": ["2026-11-05"]}', encoding="utf-8")
    monkeypatch.setattr(tu, "_HOLIDAY_PATH", path, raising=False)
    assert tu.is_trading_day(_ist("2026-11-05 10:30"))[0] is False


def test_malformed_holiday_file_does_not_crash(monkeypatch, tmp_path):
    import blue_engine.core.time_utils as tu
    path = tmp_path / "h.json"
    path.write_text("not json", encoding="utf-8")
    monkeypatch.setattr(tu, "_HOLIDAY_PATH", path, raising=False)
    assert tu.is_trading_day(_ist("2026-11-02 10:30"))[0] is True


# --------------------------------------------------------------------------- #
# Supervisor gate
# --------------------------------------------------------------------------- #
def test_market_hours_gate_blocks_outside_the_session():
    from blue_engine.core.supervisor import market_hours_gate
    assert market_hours_gate(now=_ist("2026-11-01 10:30")).ok is False
    assert market_hours_gate(now=_ist("2026-11-02 08:00")).ok is False
    assert market_hours_gate(now=_ist("2026-11-02 10:30")).ok is True


def test_market_hours_gate_can_be_overridden_explicitly():
    from blue_engine.core.supervisor import market_hours_gate
    assert market_hours_gate(now=_ist("2026-11-01 10:30"),
                             allow_outside=True).ok is True


def test_paper_runs_skip_the_hours_gate():
    from blue_engine.core.supervisor import build_default_gates

    class _C:
        api_key = "k"

    live = build_default_gates(_C(), "http://x", "INFY", "NSE", paper=False)
    paper = build_default_gates(_C(), "http://x", "INFY", "NSE", paper=True)
    assert len(live) == len(paper) + 1, "only the live chain has the hours gate"


# --------------------------------------------------------------------------- #
# API key resolution (.env)
# --------------------------------------------------------------------------- #
def test_env_file_is_loaded_when_the_process_has_no_key(monkeypatch, tmp_path):
    import blue_engine.core.client as client_mod

    env = tmp_path / ".env"
    env.write_text("OPENALGO_API_KEY=from-file-key\n", encoding="utf-8")
    monkeypatch.delenv("OPENALGO_API_KEY", raising=False)
    monkeypatch.setattr(client_mod, "_DOTENV_LOADED", False, raising=False)
    monkeypatch.setattr(client_mod, "_CACHED_KEY", None, raising=False)

    assert client_mod.load_env_file(str(env)) is True
    assert client_mod.resolve_api_key() == "from-file-key"


def test_process_environment_beats_the_file(monkeypatch, tmp_path):
    import blue_engine.core.client as client_mod

    env = tmp_path / ".env"
    env.write_text("OPENALGO_API_KEY=from-file-key\n", encoding="utf-8")
    monkeypatch.setenv("OPENALGO_API_KEY", "from-env")
    monkeypatch.setattr(client_mod, "_DOTENV_LOADED", False, raising=False)
    client_mod.load_env_file(str(env))
    assert client_mod.resolve_api_key() == "from-env"


def test_missing_env_file_is_not_an_error(monkeypatch, tmp_path):
    import blue_engine.core.client as client_mod
    assert client_mod.load_env_file(str(tmp_path / "nope.env")) is False


def test_client_without_a_key_fails_at_construction(monkeypatch, tmp_path):
    import blue_engine.core.client as client_mod

    monkeypatch.delenv("OPENALGO_API_KEY", raising=False)
    monkeypatch.setattr(client_mod, "_DOTENV_LOADED", False, raising=False)
    monkeypatch.setattr(client_mod, "_CACHED_KEY", None, raising=False)
    monkeypatch.setattr(client_mod, "load_env_file",
                        lambda *a, **k: False, raising=False)
    monkeypatch.setattr(client_mod, "resolve_api_key", lambda: None, raising=False)
    with pytest.raises(RuntimeError, match="no OpenAlgo API key"):
        client_mod.init_client()


# --------------------------------------------------------------------------- #
# Interval vocabulary (LIVE-12)
# --------------------------------------------------------------------------- #
def test_valid_intervals_pass_through():
    from blue_engine.core.client import VALID_INTERVALS, normalize_interval
    for value in VALID_INTERVALS:
        assert normalize_interval(value) == value


def test_legacy_minute_spellings_are_translated():
    from blue_engine.core.client import normalize_interval
    assert normalize_interval("15minute") == "15m"
    assert normalize_interval("5minute") == "5m"
    assert normalize_interval("1minute") == "1m"
    assert normalize_interval("60minute") == "1h"


def test_day_week_month_spellings():
    from blue_engine.core.client import normalize_interval
    assert normalize_interval("1day") == "D"
    assert normalize_interval("daily") == "D"
    assert normalize_interval("1d") == "D"
    assert normalize_interval("weekly") == "W"
    assert normalize_interval("monthly") == "M"


def test_unknown_interval_is_not_silently_substituted():
    # Changing the timeframe without telling anyone would change the strategy.
    from blue_engine.core.client import normalize_interval
    assert normalize_interval("7m") == "7m"
    assert normalize_interval("weird") == "weird"


def test_none_defaults_to_five_minute():
    from blue_engine.core.client import normalize_interval
    assert normalize_interval(None) == "5m"


def test_master_config_uses_api_valid_intervals():
    import json
    from pathlib import Path
    from blue_engine.core.client import VALID_INTERVALS

    path = Path(__file__).resolve().parents[1] / "input" / "master.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    ocl = raw.get("ocl_keylevel", {})
    for key in ("htf_interval", "ltf_interval"):
        if key in ocl:
            assert ocl[key] in VALID_INTERVALS, (
                f"master.json {key}={ocl[key]!r} is not accepted by the API")


def test_strategy_defaults_and_choices_are_api_valid():
    from blue_engine.alpha.Statergy.ocl_keylevel import OCLKeylevelSignal
    from blue_engine.core.client import VALID_INTERVALS

    space = OCLKeylevelSignal.params_space
    for key in ("htf_interval", "ltf_interval"):
        spec = space[key]
        assert spec["default"] in VALID_INTERVALS
        for choice in spec["choices"]:
            assert choice in VALID_INTERVALS, f"{key} choice {choice!r} is invalid"


def test_signal_normalizes_a_legacy_config_value():
    from blue_engine.alpha.Statergy.ocl_keylevel import OCLKeylevelSignal

    class _C:
        pass

    sig = OCLKeylevelSignal(_C(), {"symbol": "INFY", "exchange": "NSE",
                                   "htf_interval": "15minute",
                                   "ltf_interval": "5minute"})
    assert sig.htf_interval == "15m"
    assert sig.ltf_interval == "5m"
