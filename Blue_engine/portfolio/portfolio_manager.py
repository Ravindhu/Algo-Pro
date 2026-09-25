"""
portfolio_manager.py — Portfolio Manager API shell (M1 of the manager plan).

Role
----
A thin, *stable* public API in front of the Portfolio Engine, designed so the
Dash dashboard (and anything else — CLI, scheduler, tests) talks to ONE object
instead of touching ``PortfolioEngine`` internals. Swapping the underlying
engine does not break callers: they talk to ``PortfolioManager``.

Public methods (the contract the dashboard uses)::

    pm = PortfolioManager()          # registry chooses the impl
    pm.get_dashboard_data()          # one call -> everything the /portfolio page needs
    pm.get_regime()                  # Economic regime + gross multiplier
    pm.get_tearsheet()               # Sharpe/Sortino/Calmar/DD from stored runs
    pm.refresh_portfolio()           # force a fresh engine run (new data)
    pm.run_optimization("hrp")       # run one named optimiser, compare weights

    # Phase 1 — the equity curve (foundation for all later analytics)
    pm.ingest_trades(recent)         # book new fills (idempotent across polls)
    pm.snapshot_nav(marks)           # freeze + persist one end-of-day NAV row
    pm.get_equity_curve()            # NAV path + daily returns for Phases 2-5

Why the tracker lives here
--------------------------
``EquityTracker`` (``portfolio/tracker.py``) owns the accounting; this class
only *drives* it. That keeps the maths unit-testable in isolation while the
dashboard gets one object to talk to.

Degradation contract ("degrade, never halt")
--------------------------------------------
* live data unavailable -> cached provider snapshot -> empty book (never raise).
* every method returns a plain dict with ``"ok": True/False`` + ``"warnings"``.
* no numpy / no provider / no key — everything still works (template paths).

Registry pattern
----------------
``register("default", PortfolioManager)`` lets a future implementation (e.g.
one backed by a DB or an execution layer) replace this one without touching the
dashboard: ``PortfolioManager.create()`` reads ``config["manager_impl"]``.

Example
-------
>>> from blue_engine.portfolio import portfolio_manager as pm_mod
>>> pm = pm_mod.PortfolioManager.create({"optimiser": "risk_parity"})
>>> snap = pm.get_dashboard_data()
>>> snap["ok"], snap["data"]["optimiser"]
(True, 'risk_parity')

Security: this layer renders/exposes aggregate numbers only (weights, vol,
drift). No keys, no account numbers, no PII — the provider layer already
strips credentials.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable, Dict, List, Optional

from blue_engine.core.results import safe_float
from blue_engine.portfolio import (
    attribution, optim, portfolio_state, tracker as tracker_mod)
from blue_engine.portfolio.portfolio_engine import PortfolioEngine

_log = logging.getLogger("portfolio.manager")

# --------------------------------------------------------------------------- #
#  Registry — swap manager implementations without touching callers
# --------------------------------------------------------------------------- #
_REGISTRY: Dict[str, type] = {}
_REGISTRY_LOCK = threading.Lock()


def register(name: str, cls: type) -> None:
    """Register a manager implementation, e.g. ``register("v2", ManagerV2)``."""
    with _REGISTRY_LOCK:
        _REGISTRY[str(name).lower()] = cls


def registered() -> List[str]:
    """Names currently registered (useful for a settings/debug page)."""
    with _REGISTRY_LOCK:
        return sorted(_REGISTRY)


def _empty_regime() -> Dict[str, Any]:
    """The regime snapshot used when the Economic engine has not run yet."""
    return {"ok": True, "regime": "UNKNOWN", "risk_on_score": None,
            "gross_exposure_multiplier": None, "warnings": ["no economic data"]}


# --------------------------------------------------------------------------- #
#  Default implementation
# --------------------------------------------------------------------------- #
class PortfolioManager:
    """Default API shell wrapping :class:`PortfolioEngine`."""

    name = "default"

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        self.config: Dict[str, Any] = dict(config or {})
        self._engine = PortfolioEngine(self.config)
        self._last_run: Optional[Dict[str, Any]] = None   # cache of EngineResult.data
        self._last_run_ts: float = 0.0
        self._lock = threading.Lock()
        # Phase 1: the equity/P&L accounting core. Pure object; the NAV file is
        # written only from snapshot_nav(), so a dashboard poll never mutates disk
        # unless the caller asks for a snapshot.
        #
        # NOTE the explicit ``is None`` checks: ``0.0`` is a *legitimate* config
        # value (a paper-trading account pays no charges), so it must not be
        # coerced to the default by Python's falsy-zero rule.
        _cash = safe_float(self.config.get("starting_cash"))
        _bps = safe_float(self.config.get("charges_bps"))
        self.tracker = tracker_mod.EquityTracker(
            starting_cash=0.0 if _cash is None else _cash,
            charges_bps=(tracker_mod.DEFAULT_CHARGES_BPS if _bps is None
                         else _bps),
            log=self.config.get("log"))
        self._seen_fills: set = set()      # content keys of booked fills

    # -- construction ---------------------------------------------------- #
    @classmethod
    def create(cls, config: Optional[Dict[str, Any]] = None) -> "PortfolioManager":
        """Factory honouring ``config["manager_impl"]`` (registry pattern)."""
        with _REGISTRY_LOCK:
            impl = _REGISTRY.get(str((config or {}).get("manager_impl", "default")).lower())
        if impl and impl is not cls:
            return impl(config)  # type: ignore[call-arg]
        return cls(config)

    # -- core run -------------------------------------------------------- #
    def refresh_portfolio(self, context: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Force a fresh engine run (new provider fetch) and cache the result.

        Returns the engine's ``data`` dict plus ``ok``/``warnings``; never raises.
        """
        try:
            with self._lock:
                res = self._engine.run(context or {})
                data = dict(res.data)
                data["ok"] = bool(res.ok)
                data["warnings"] = list(res.warnings)
                if self.config.get("persist", True):
                    try:
                        portfolio_state.record(
                            {"optimiser": data.get("optimiser"),
                             "target_weights": data.get("target_weights"),
                             "risk": data.get("risk")})
                    except Exception as exc:  # noqa: BLE001
                        data.setdefault("warnings", []).append(
                            f"persist failed: {exc}")
                self._last_run = data
                self._last_run_ts = time.time()
                return data
        except Exception as exc:  # noqa: BLE001
            _log.exception("refresh_portfolio failed")
            stale = self._last_run or {}
            return {**stale, "ok": False,
                    "warnings": [f"engine failed ({exc}); serving last known"]}

    # -- reads (fall back to cache, never raise) -------------------------- #
    def get_regime(self, context: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Economic regime snapshot the portfolio is currently sized against."""
        ctx = context or {}
        econ = (ctx.get("economic") or {}).get("data") if isinstance(
            ctx.get("economic"), dict) else None
        if not isinstance(econ, dict) or not econ:
            last = self._last_run or {}
            regime = last.get("regime")
            if regime:
                return {"ok": True, "regime": regime,
                        "risk_on_score": last.get("risk_on_score"),
                        "gross_exposure_multiplier":
                            last.get("gross_exposure_multiplier"),
                        "warnings": ["from last portfolio run"]}
            return _empty_regime()
        return {"ok": True, "regime": econ.get("regime", "UNKNOWN"),
                "risk_on_score": econ.get("risk_on_score"),
                "gross_exposure_multiplier": econ.get("gross_exposure_multiplier"),
                "warnings": []}

    def get_dashboard_data(self, context: Optional[Dict[str, Any]] = None,
                           refresh: bool = False) -> Dict[str, Any]:
        """One call -> everything the /portfolio Dash page needs.

        Keys: ``regime``, ``target_weights``, ``rebalance_trades``, ``risk``,
        ``hedge``, ``optimiser``, ``cost``, ``sizing``, ``drift``, ``ok``.
        """
        if refresh or self._last_run is None:
            run = self.refresh_portfolio(context)
        else:
            run = dict(self._last_run)
        regime = self.get_regime(context)
        if run.get("ok") is False:
            return {**run, "regime": regime}
        actual = (run.get("current_weights")
                  or (self._last_run or {}).get("current_weights") or {})
        try:
            drift = attribution.drift.drift(run.get("target_weights") or {},
                                            actual)
        except Exception:  # noqa: BLE001
            drift = {"sleeves": {}, "total_abs_drift": None}
        return {"ok": True,
                "as_of": self._last_run_ts,
                "regime": regime,
                "target_weights": run.get("target_weights", {}),
                "current_weights": actual,
                "rebalance_trades": run.get("rebalance_trades", []),
                "risk": run.get("risk", {}),
                "hedge": run.get("hedge", {}),
                "optimiser": run.get("optimiser"),
                "cost": run.get("cost", {}),
                "sizing": run.get("sizing", {}),
                "drift": drift,
                "warnings": run.get("warnings", [])}

    def get_tearsheet(self, days: int = 90) -> Dict[str, Any]:
        """Full M2 tearsheet: weights/turnover/drift/HHI/gross from stored runs."""
        try:
            snaps = portfolio_state.snapshots(days=days)
            rows = portfolio_state.read_recent(days=days)
            ts = attribution.tearsheet.build(snaps, rows or [])
            out = {"ok": ts.get("ok", False), "days": days,
                   "runs": ts.get("runs", len(snaps)),
                   "target_weights": (snaps[-1].get("target") if snaps else {}) or {}}
            for k, v in ts.items():
                if k != "ok":
                    out[k] = v
            return out
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "days": days, "runs": 0,
                    "warnings": [f"tearsheet unavailable: {exc}"]}

    # -- Phase 1: equity curve ------------------------------------------- #
    @staticmethod
    def _fill_key(t: Any) -> Optional[tuple]:
        """A content hash identifying one fill, for de-duplication.

        Why content and not a count: a caller may hand us a *growing* list (the
        dashboard's ``recent_trades(200)`` window) or a *single new* fill. A
        positional cursor breaks the second case and can double-book the first
        when the window slides. Keying on content is correct for both.

        Returns None when the row lacks the fields needed to be a fill, so
        malformed rows are skipped rather than colliding on a shared key.
        """
        if isinstance(t, dict):
            g = t.get
        else:
            g = lambda k, _d=None, _t=t: getattr(_t, k, _d)  # noqa: E731
        sym = str(g("symbol", "") or "")
        side = str(g("side", "") or "").upper()
        qty = safe_float(g("qty"))
        px = safe_float(g("price"))
        if not sym or side not in ("BUY", "SELL") or not qty or px is None:
            return None
        exch = str(g("exchange", "") or "")
        ts = safe_float(g("ts")) or 0.0
        return (sym, exch, side, int(qty), round(float(px), 6), round(ts, 3))

    def ingest_trades(self, trades: Any, *,
                      default_exchange: str = "") -> Dict[str, Any]:
        """Book fills into the equity tracker, de-duplicated by content.

        Safe to call on every dashboard poll: a fill already booked is ignored,
        whether the caller sends the same window again or a single new fill.

        Returns ``{"ok", "applied", "skipped", "total_ingested", "tracker"}``;
        never raises.
        """
        try:
            applied = skipped = 0
            for t in list(trades or []):
                key = self._fill_key(t)
                if key is None:
                    skipped += 1
                    continue
                if key in self._seen_fills:
                    skipped += 1
                    continue
                if isinstance(t, dict):
                    g = t.get
                    exch = str(t.get("exchange") or default_exchange)
                else:
                    g = lambda k, _d=None, _t=t: getattr(_t, k, _d)  # noqa: E731
                    exch = str(g("exchange", default_exchange)
                               or default_exchange)
                if self.tracker.on_fill(str(g("symbol", "") or ""), exch,
                                        str(g("side", "") or ""),
                                        int(safe_float(g("qty")) or 0),
                                        safe_float(g("price")) or 0.0,
                                        ts=safe_float(g("ts"))):
                    self._seen_fills.add(key)
                    applied += 1
                else:
                    skipped += 1
            return {"ok": True, "applied": applied, "skipped": skipped,
                    "total_ingested": len(self._seen_fills),
                    "tracker": self.tracker.describe()}
        except Exception as exc:  # noqa: BLE001 - a poll must never break the page
            _log.warning("ingest_trades failed: %r", exc)
            return {"ok": False, "applied": 0, "skipped": 0,
                    "total_ingested": len(self._seen_fills),
                    "warnings": [f"trade ingestion failed: {exc}"]}

    def snapshot_nav(self, marks: Optional[Dict[str, float]] = None, *,
                     ts: Optional[float] = None,
                     persist: bool = True,
                     extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Freeze one NAV row and (optionally) append it to the equity curve.

        Called once per session (the post-close job) with end-of-day marks. Pass
        ``ts`` to date the row explicitly (e.g. a backfill or a simulated day);
        it defaults to *now*, which is what the live job wants.

        Any telemetry in ``extra`` (regime, optimiser, gross) is merged so later
        analysis can slice the curve by regime without a join.

        Returns the snapshot dict plus ``{"ok", "stored"}``; never raises.
        """
        try:
            marks = dict(marks or {})
            row = self.tracker.snapshot(marks, ts=ts, extra=extra)
            stored_to = None
            if persist:
                stored_to = tracker_mod.store_nav(
                    row, directory=self.config.get("nav_dir"))
            return {**row, "ok": True,
                    "stored": str(stored_to) if stored_to else None}
        except Exception as exc:  # noqa: BLE001
            _log.warning("snapshot_nav failed: %r", exc)
            return {"ok": False, "warnings": [f"nav snapshot failed: {exc}"]}

    def get_equity_curve(self, months: int = 12) -> Dict[str, Any]:
        """The stored daily equity curve + derived daily returns.

        This is the Phase-2 input: Phases 2–5 read this instead of recomputing
        P&L, which keeps one definition of "return" across the whole desk.
        """
        try:
            rows = tracker_mod.read_nav(
                months=months, directory=self.config.get("nav_dir"))
            rets = tracker_mod.daily_returns(rows)
            return {"ok": True, "rows": rows, "n": len(rows),
                    "returns": rets, "n_returns": len(rets),
                    "dates": [r.get("date") for r in rows],
                    "nav": [safe_float(r.get("nav")) for r in rows]}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "rows": [], "returns": [],
                    "warnings": [f"equity curve unavailable: {exc}"]}

    def run_optimization(self, method: str,
                         context: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Run a *named* optimiser (``hrp``, ``erc``/``risk_parity``, ``bl``…)
        against the latest inputs, and compare weights to the current pick."""
        base = self.get_dashboard_data(context, refresh=True)
        if base.get("ok") is not True:
            return {"ok": False, "warnings": base.get("warnings", [])}
        try:
            inputs = self._engine._collect(context or {}) or {}
            prob = {"universe": list(inputs.get("universe") or []),
                    "cov": inputs.get("cov"), "vols": inputs.get("vols") or {},
                    "constraints": {"max_weight": self._engine.max_weight,
                                    "max_gross": self._engine.max_gross}}
            out = optim.registry.optimise(method, prob)
            return {"ok": bool(out.get("ok")), "method": method,
                    "weights": out.get("weights", {}),
                    "current_pick": base.get("optimiser"),
                    "current_weights": base.get("target_weights", {}),
                    "warnings": out.get("warnings", [])}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "method": method,
                    "warnings": [f"optimisation failed: {exc}"]}


# Register the default implementation so ``create()`` is discoverable.
register("default", PortfolioManager)
