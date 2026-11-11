"""Live-trading compliance gates. Every check must pass before a live order may be sent.

Flow stage: Compliance gate, Pre-gate safety, Live intraday VaR/CVaR
Status: PHASE 3 IMPLEMENTED (2026-11-11)

Why this exists
---------------
Live-order preconditions (owner decision 2026-11-11: the SEBI registration / exchange algo-ID
fields are NOT required here — OpenAlgo tags orders with the strategy id, and registration is
the broker's obligation, not a code gate):
  1. Records of algo orders are retained and available for inspection (audit-log storage).
  2. Order timestamps are accurate: the host clock must be synchronised (NTP), because the
     exchange matches order time against its own clock and a skewed clock breaks the audit trail.

These checks are LIVE-ONLY. Paper and analyze modes never send a real order, so they are not
gated here. Each check fails closed: unknown means blocked.

This module never sends an order and never imports an order client.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

__all__ = ["ComplianceConfig", "ComplianceResult", "ComplianceGate", "check_audit_storage"]


@dataclass(frozen=True)
class ComplianceConfig:
    # Audit-log storage: the directory that must exist and be writable.
    audit_dir: str = "data/audit"
    min_free_bytes: int = 50 * 1024 * 1024       # 50 MB headroom before live trading
    # NTP: maximum allowed clock skew in seconds.
    max_clock_skew_seconds: float = 1.0


@dataclass
class ComplianceResult:
    ok: bool
    reasons: Tuple[str, ...] = ()
    detail: Dict[str, object] = field(default_factory=dict)


def check_audit_storage(audit_dir, min_free_bytes: int = 0) -> ComplianceResult:
    """The audit directory must exist, be writable, and have free space. Fail closed."""
    import shutil

    path = Path(audit_dir)
    reasons: List[str] = []
    if not path.is_dir():
        reasons.append(f"audit dir missing: {path}")
    else:
        probe = path / ".write_probe"
        try:
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
        except Exception as exc:
            reasons.append(f"audit dir not writable: {exc!r}")
        try:
            free = shutil.disk_usage(str(path)).free
            if free < min_free_bytes:
                reasons.append(f"audit storage low: {free} bytes free, need {min_free_bytes}")
        except Exception as exc:
            reasons.append(f"audit storage size unknown: {exc!r}")
    return ComplianceResult(not reasons, tuple(reasons))


class ComplianceGate:
    """Live-only compliance checks. Call check() before the first live order of the session."""

    def __init__(self, cfg: ComplianceConfig = ComplianceConfig(),
                 clock_check: Optional[Callable[[], dict]] = None) -> None:
        self.cfg = cfg
        self._clock_check = clock_check

    def audit_storage_ok(self) -> ComplianceResult:
        return check_audit_storage(self.cfg.audit_dir, self.cfg.min_free_bytes)

    def clock_ok(self) -> ComplianceResult:
        """NTP clock sync. No checker injected means unknown, which fails closed."""
        if self._clock_check is None:
            return ComplianceResult(False, ("clock check not configured: NTP sync unverified",))
        try:
            res = self._clock_check()
        except Exception as exc:
            return ComplianceResult(False, (f"clock check failed: {exc!r}",))
        if not isinstance(res, dict) or not res.get("ok"):
            skew = (res or {}).get("skew_seconds") if isinstance(res, dict) else None
            return ComplianceResult(False, (f"clock not synced (skew={skew})",), detail=dict(res or {}))
        return ComplianceResult(True, detail=dict(res))

    def check(self) -> ComplianceResult:
        results = [self.audit_storage_ok(), self.clock_ok()]
        reasons = tuple(r for res in results for r in res.reasons)
        detail: Dict[str, object] = {}
        for res in results:
            detail.update(res.detail)
        return ComplianceResult(not reasons, reasons, detail)
