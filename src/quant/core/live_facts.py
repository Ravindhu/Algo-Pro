"""Gather the live-checklist facts from repo files and the kill switch (decision 0031).

Reads only local files. OpenAlgo mode, safe mode, loss breach and reconciliation are NOT read here yet,
so they are None and fail their checks (fail closed) until those readers are wired in.

Flow stage: Dashboard live switch
Status: PHASE 3 IMPLEMENTED (local facts only)
"""
import hashlib
from datetime import datetime
from pathlib import Path

import yaml

from quant.core.calendar import TradingCalendar
from quant.core.config import RiskLimits
from quant.core.live_checklist import LiveFacts
from quant.risk.kill_switch import KillSwitch


def gather(root, kill_path, approver, now_ist: datetime) -> LiveFacts:
    root = Path(root)
    cal = TradingCalendar.from_csv(root / "config" / "calendar" / "holidays.csv")
    limits_path = root / "config" / "risk_limits.yaml"
    sha = hashlib.sha256(limits_path.read_bytes()).hexdigest()
    limits = RiskLimits.from_yaml(limits_path)
    sources = (yaml.safe_load((root / "config" / "data_sources.yaml").read_text(encoding="utf-8")) or {})
    sources = sources.get("sources", {})
    licensed = any(s.get("live_allowed") is True and bool(s.get("licence")) for s in sources.values())
    return LiveFacts(
        calendar_verified=cal.verified,
        is_trading_day=cal.is_trading_day(now_ist.date()),
        now_ist=now_ist,
        data_source_licensed=licensed,
        approver=approver,
        approval_limits_sha256=sha,      # the approval is made now, against the current file
        current_limits_sha256=sha,
        max_symbols_per_portfolio=limits.max_symbols_per_portfolio,
        openalgo_mode=None,
        kill_active=KillSwitch(kill_path).active,
        safe_mode_halt=None,
        daily_loss_breach=None,
        recon_clean=None,
    )
