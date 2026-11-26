"""Audit fix 4 (2026-11-26): owner sign-off, holiday coverage, no entries on non-trading days. Offline."""
from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

import pytest

from quant.core import signoff
from quant.core.calendar import TradingCalendar, calendar_coverage
from quant.core.config import RiskLimits
from quant.execution.position_store import PositionStore
from quant.execution.strategy_bot import BotConfig, StrategyBot
from quant.monitor.drill import run_drill
from quant.monitor.safe_mode import SafeMode
from quant.risk.controls import HaltHandler
from quant.risk.gate import RiskState
from quant.risk.kill_switch import KillSwitch

ROOT = Path(__file__).resolve().parents[3]
LIMITS = ROOT / "config" / "risk_limits.yaml"
TODAY = date(2026, 11, 26)


def _good(fp="FP", **over):
    rec = dict(fingerprint=fp, drill_ok=True, soak_sessions=5, soak_notes="ok",
               approved_by="owner", today=TODAY)
    rec.update(over)
    return signoff.build_signoff(**rec)


# ---- sign-off record -----------------------------------------------------------------------

def test_a_good_signoff_passes_for_the_same_config(tmp_path):
    f = tmp_path / "signoff.json"
    signoff.save_signoff(f, _good(fp="FP"))
    ok, reasons = signoff.check_signoff(f, fingerprint="FP", today=TODAY)
    assert ok and reasons == []


def test_missing_or_unreadable_signoff_refuses(tmp_path):
    ok, reasons = signoff.check_signoff(tmp_path / "nope.json", fingerprint="FP", today=TODAY)
    assert not ok and "no owner sign-off" in reasons[0]
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    ok, reasons = signoff.check_signoff(bad, fingerprint="FP", today=TODAY)
    assert not ok and "unreadable" in reasons[0]


def test_config_change_invalidates_the_signoff(tmp_path):
    f = tmp_path / "signoff.json"
    signoff.save_signoff(f, _good(fp="OLD"))
    ok, reasons = signoff.check_signoff(f, fingerprint="NEW", today=TODAY)
    assert not ok and any("different config" in r for r in reasons)


def test_old_and_future_signoffs_refuse(tmp_path):
    f = tmp_path / "signoff.json"
    signoff.save_signoff(f, _good(fp="FP"))
    ok, reasons = signoff.check_signoff(f, fingerprint="FP", today=date(2026, 12, 20))
    assert not ok and any("days old" in r for r in reasons)
    ok, reasons = signoff.check_signoff(f, fingerprint="FP", today=date(2026, 11, 1))
    assert not ok and any("future" in r for r in reasons)


def test_too_few_sessions_or_no_drill_or_no_approver_refuse(tmp_path):
    f = tmp_path / "signoff.json"
    signoff.save_signoff(f, _good(fp="FP"))
    rec = signoff.json.loads(f.read_text())
    for field, value, text in (("soak_sessions", 2, "fewer than"), ("drill_passed", False, "drill"),
                               ("approved_by", "", "approver")):
        rec2 = dict(rec, **{field: value})
        signoff.save_signoff(f, rec2)
        ok, reasons = signoff.check_signoff(f, fingerprint="FP", today=TODAY)
        assert not ok and any(text in r for r in reasons), field


def test_build_refuses_a_record_that_could_never_pass():
    with pytest.raises(ValueError, match="drill"):
        _good(drill_ok=False)
    with pytest.raises(ValueError, match="clean analyze"):
        _good(soak_sessions=4)
    with pytest.raises(ValueError, match="approved_by"):
        _good(approved_by="   ")


def test_fingerprint_changes_with_any_config_byte(tmp_path):
    a = tmp_path / "a.yaml"
    a.write_text("x: 1\n", encoding="utf-8")
    fp1 = signoff.config_fingerprint([a])
    a.write_text("x: 2\n", encoding="utf-8")
    assert signoff.config_fingerprint([a]) != fp1


# ---- holiday coverage ----------------------------------------------------------------------

def test_coverage_passes_only_when_next_30_days_are_inside_the_verified_range(tmp_path):
    c = tmp_path / "coverage.yaml"
    c.write_text("covered_through: 2026-12-31\n", encoding="utf-8")
    assert calendar_coverage(c, date(2026, 11, 26))[0] is True
    ok, reasons = calendar_coverage(c, date(2026, 12, 5))
    assert ok is False and "only verified through 2026-12-31" in reasons[0]


def test_missing_coverage_file_refuses(tmp_path):
    ok, reasons = calendar_coverage(tmp_path / "none.yaml", TODAY)
    assert ok is False and "missing" in reasons[0]


def test_repo_coverage_file_is_present_and_readable():
    ok, _ = calendar_coverage(ROOT / "config" / "calendar" / "coverage.yaml", TODAY)
    assert ok is True


# ---- no entries on non-trading days ---------------------------------------------------------

class _NoBars:
    def bars(self, sym, now, exchange=None):
        return None

    def daily_stats(self, sym, now):
        return None


class _Audit:
    def __init__(self):
        self.rows = []

    def append(self, kind, payload):
        self.rows.append((kind, payload))


def _bot(tmp_path, day_is_trading):
    (tmp_path / "data.yaml").write_text("sources:\n  live_feed:\n    live_allowed: true\n", encoding="utf-8")
    cfg = BotConfig(mode="paper", data_sources_path=str(tmp_path / "data.yaml"),
                    screen_path=str(tmp_path / "none.json"))
    return StrategyBot(
        cfg=cfg, strategies=[], bars=_NoBars(), sender=type("S", (), {"bot_mode": "paper"})(),
        audit=_Audit(), api_key="KEY", read_funds=lambda: 1_000_000.0, limits=RiskLimits.from_yaml(LIMITS),
        kill=KillSwitch(tmp_path / "kill.json"), safe=SafeMode(), halt=HaltHandler(), state=RiskState(),
        position_store=PositionStore(tmp_path / "pos.json"), day_is_trading=day_is_trading,
    )


def test_non_trading_day_blocks_entries_with_a_reason(tmp_path):
    bot = _bot(tmp_path, day_is_trading=lambda d: False)
    report = bot.cycle(datetime(2026, 11, 28, 10, 0))  # a Saturday
    assert any("non-trading day" in s for s in report["skipped"])


def test_trading_day_does_not_block_for_the_calendar(tmp_path):
    bot = _bot(tmp_path, day_is_trading=lambda d: True)
    report = bot.cycle(datetime(2026, 11, 26, 10, 0))
    assert not any("non-trading day" in s for s in report["skipped"])


def test_calendar_error_counts_as_non_trading(tmp_path):
    def boom(d):
        raise RuntimeError("calendar broke")
    bot = _bot(tmp_path, day_is_trading=boom)
    report = bot.cycle(datetime(2026, 11, 26, 10, 0))
    assert any("non-trading day" in s for s in report["skipped"])


def test_real_calendar_knows_christmas_and_weekends():
    cal = TradingCalendar.from_csv(ROOT / "config" / "calendar" / "holidays.csv")
    assert cal.is_trading_day(date(2026, 11, 26)) is True
    assert cal.is_trading_day(date(2026, 12, 25)) is False   # in the CSV
    assert cal.is_trading_day(date(2026, 11, 28)) is False   # Saturday


# ---- the drill runs and passes (offline) ----------------------------------------------------

def test_safety_drill_passes_offline(tmp_path):
    from quant.audit.log import AuditLog
    report = run_drill(tmp_path / "drill", audit=AuditLog(tmp_path / "audit.jsonl"))
    assert report.ok, [p.name for p in report.phases if not p.passed]
