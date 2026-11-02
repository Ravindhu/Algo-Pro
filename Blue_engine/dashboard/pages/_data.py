"""
_data.py - shared, read-only data adapter for the Dash pages.

The Dash pages must not care how the engine got its numbers. This module
is the one place that knows:

  * how to load the user inputs (``blue_engine/input/master.json`` and any
    override file), so every page shows the *same* inputs the slicer uses;
  * how to read running executions / VWAP / slippage from the ledger
    (:mod:`blue_engine.core.ledger`), which mirrors to the DB when available;
  * how to fall back to a live in-process snapshot when the dashboard is
    hosted in the same process as :class:`LiveDashboard`.

Everything is best-effort: if the ledger or config is missing we return
empty structures and a flag, never raise. A dashboard that dies because a
DB is down is worse than a dashboard with blank tables.
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from blue_engine.core.mode import read_mode

logger = logging.getLogger(__name__)


# Default config location, relative to the repo root.
_DEFAULT_CONFIG = Path("blue_engine") / "input" / "master.json"


def config_path() -> Path:
    """Resolve the config path (env override wins)."""
    env = os.getenv("POV_CONFIG_PATH")
    if env:
        return Path(env)
    return _DEFAULT_CONFIG


def load_config(path: Optional[Path] = None) -> Dict[str, Any]:
    """Load the dashboard/slicer config JSON.

    Returns ``{}`` when the file is missing or malformed so callers can
    treat "no config" and "empty config" identically.
    """
    p = Path(path) if path else config_path()
    try:
        with p.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        logger.debug("config not found at %s", p)
        return {}
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("config load failed at %s: %s", p, exc)
        return {}


def _pov_block(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """Return the flat POV input block from the config.

    ``master.json`` in this repo keeps the POV inputs at the top level
    (alongside the other Feature knobs). A hand-written dashboard config
    may instead nest them under a ``pov`` key. Support both so the pages
    work with either shape.
    """
    if not isinstance(cfg, dict):
        return {}
    nested = cfg.get("pov")
    if isinstance(nested, dict):
        merged = dict(cfg)
        merged.pop("pov", None)
        merged.update(nested)
        return merged
    return cfg


@dataclass
class InputRow:
    """One user input rendered as a table row."""

    group: str
    key: str
    value: Any
    source: str  # "master.json" | "env" | "default"


@dataclass
class Snapshot:
    """A point-in-time read of everything the pages need."""

    config: Dict[str, Any] = field(default_factory=dict)
    inputs: List[InputRow] = field(default_factory=list)
    executions: List[Dict[str, Any]] = field(default_factory=list)
    positions: List[Dict[str, Any]] = field(default_factory=list)
    trades: List[Dict[str, Any]] = field(default_factory=list)
    alerts: List[Dict[str, Any]] = field(default_factory=list)
    ledger_ok: bool = False
    live_engine: Any = None
    fetched_at: float = 0.0
    mode: str = "unknown"  # "live" | "analyze" | "unknown" (OpenAlgo analyzer mode)

    def pov(self) -> Dict[str, Any]:
        return _pov_block(self.config)


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------

# Which top-level inputs belong to which page group. This is what lets
# the Terminal page show "runtime" inputs and the Risk page show the
# risk inputs, from a single flat config.
_INPUT_GROUPS: Dict[str, tuple] = {
    "runtime": (
        "pov_mode", "mode", "action", "symbol", "exchange", "product",
        "order_mode", "quantity", "interval", "duration", "starting_cash",
    ),
    "pov_band": (
        "vwap_filter_enabled", "vwap_ceiling_pct", "vwap_floor_pct",
        "min_pov_pct", "max_pov_pct", "passthrough_floor",
        "vwap_session_floor_price",
    ),
    "slippage": (
        "slippage_guard_enabled", "max_slippage_bps",
        "slippage_penalty_factor", "slippage_window_fills",
        "arrival_window_ticks",
    ),
    "risk": (
        "max_universe_rows", "quote_failure_threshold",
        "ceiling_price", "stop_loss_price", "upper_circuit", "lower_circuit",
        "max_symbols", "max_blocks_pct", "cancellation_cool_off_seconds",
    ),
}

# Keys the dashboard treats as secret-ish; never render their value.
_MASKED_KEYS = ("api_key", "apikey", "secret", "password", "token")


def group_for_key(key: str) -> str:
    for group, keys in _INPUT_GROUPS.items():
        if key in keys:
            return group
    return "other"


def _mask(key: str, value: Any) -> Any:
    if any(s in key.lower() for s in _MASKED_KEYS) and value:
        return "********"
    return value


def collect_inputs(cfg: Dict[str, Any]) -> List[InputRow]:
    """Flatten the config into a list of labelled input rows."""
    flat = _pov_block(cfg)
    rows: List[InputRow] = []
    for key in sorted(flat.keys()):
        if key.startswith("_"):  # comment keys
            continue
        rows.append(
            InputRow(
                group=group_for_key(key),
                key=key,
                value=_mask(key, flat[key]),
                source="master.json",
            )
        )
    return rows


# ---------------------------------------------------------------------------
# Ledger
# ---------------------------------------------------------------------------


def _try_ledger() -> tuple[bool, Optional[Any]]:
    try:
        from blue_engine.core import ledger as _ledger

        return bool(_ledger.ledger_available()), _ledger
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("ledger unavailable: %s", exc)
        return False, None


def _execution_rows(ledger: Any) -> List[Dict[str, Any]]:
    """Best-effort enumeration of running executions.

    The ledger is keyed by execution_id and has no public "list" API, so
    we look in the in-memory registry (populated by create_execution) and
    hydrate each row through the public get_vwap / slippage accessors.
    """
    rows: List[Dict[str, Any]] = []
    store = getattr(ledger, "_IN_MEMORY_EXECUTIONS", None)
    if not isinstance(store, dict):
        return rows
    for execution_id in list(store.keys()):
        try:
            row = ledger.get_execution(execution_id) or {}
        except Exception:
            continue
        if not isinstance(row, dict):
            row = {}
        vwap = None
        slip = 0.0
        try:
            vwap = ledger.get_vwap(execution_id)
            slip = ledger.recent_slippage_bps(execution_id)
        except Exception:
            pass
        rows.append(
            {
                "execution_id": execution_id,
                "strategy": row.get("strategy") or row.get("request", {}).get("strategy") or "-",
                "symbol": row.get("symbol") or row.get("request", {}).get("symbol") or "-",
                "exchange": row.get("exchange") or row.get("request", {}).get("exchange") or "-",
                "action": row.get("action") or row.get("request", {}).get("action") or "-",
                "phase": row.get("phase", "accumulating"),
                "target_qty": row.get("target_quantity") or row.get("quantity") or 0,
                "filled_qty": row.get("filled_quantity") or 0,
                "vwap": vwap,
                "slippage_bps": slip,
                "status": row.get("status", "running"),
            }
        )
    return rows


def _live_snapshot(engine: Any) -> Dict[str, List[Dict[str, Any]]]:
    """Pull rows out of a live LiveDashboard instance, if one is attached."""
    out: Dict[str, List[Dict[str, Any]]] = {"positions": [], "trades": [], "alerts": []}
    if engine is None:
        return out
    try:
        out["positions"] = engine._build_universe_rows()  # noqa: SLF001 - read-only view
    except Exception as exc:
        logger.debug("live universe read failed: %s", exc)
    try:
        for t in engine._positions.recent_trades(limit=200):  # noqa: SLF001
            out["trades"].append(
                {
                    "symbol": t.symbol,
                    "exchange": t.exchange,
                    "side": t.side,
                    "qty": t.qty,
                    "price": t.price,
                    "state": t.state,
                    "pov_pct": t.pov_pct,
                    "ts": t.ts,
                }
            )
    except Exception as exc:
        logger.debug("live trades read failed: %s", exc)
    try:
        for a in engine._data.state.last_alerts[-200:]:  # noqa: SLF001
            out["alerts"].append(
                {
                    "ts": getattr(a, "ts", 0.0),
                    "severity": getattr(a, "severity", "info"),
                    "message": getattr(a, "message", repr(a)),
                }
            )
    except Exception as exc:
        logger.debug("live alerts read failed: %s", exc)
    return out


def gather(engine: Any = None, config: Optional[Dict[str, Any]] = None) -> Snapshot:
    """Read everything the pages need in one call."""
    cfg = config if config is not None else load_config()
    ok, ledger = _try_ledger()
    snap = Snapshot(config=cfg, ledger_ok=ok, live_engine=engine, fetched_at=time.time())
    snap.mode = read_mode(getattr(engine, "client", None))
    snap.inputs = collect_inputs(cfg)
    if ledger is not None:
        snap.executions = _execution_rows(ledger)
    live = _live_snapshot(engine)
    snap.positions = live["positions"]
    snap.trades = live["trades"]
    snap.alerts = live["alerts"]
    return snap


# ---------------------------------------------------------------------------
# Small formatting helpers shared by the pages
# ---------------------------------------------------------------------------


def fmt(value: Any, default: str = "-") -> str:
    """Format a value for a table cell without blowing up on None."""
    if value is None:
        return default
    if isinstance(value, float):
        return f"{value:,.2f}"
    if isinstance(value, int):
        return f"{value:,}"
    return str(value)


# ---------------------------------------------------------------------------
# Economic engine (macro regime) - read-only helpers for the Economic page
# ---------------------------------------------------------------------------

# The economic pipeline config is separate from the POV master config; it lives
# in input/macro_master.json (the six-engine macro pipeline).
_DEFAULT_MACRO_CONFIG = Path("blue_engine") / "input" / "macro_master.json"


def macro_config_path() -> Path:
    """Resolve the macro-pipeline config path (env override wins)."""
    env = os.getenv("BLUE_MACRO_CONFIG_PATH")
    if env:
        return Path(env)
    return _DEFAULT_MACRO_CONFIG


def load_macro_config(path: Optional[Path] = None) -> Dict[str, Any]:
    """Load the macro pipeline config (returns {} on any failure)."""
    p = Path(path) if path else macro_config_path()
    try:
        with p.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        logger.debug("macro config not found at %s", p)
        return {}
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("macro config load failed at %s: %s", p, exc)
        return {}


def economic_history(limit: int = 400) -> List[Dict[str, Any]]:
    """Read the persisted economic-engine runs (oldest first).

    Backed by ``blue_engine.economics.run_history`` (data/economic_runs/*.jsonl).
    Best-effort: any import/IO failure yields an empty list, never an error.
    """
    try:
        from blue_engine.economics import run_history
        return run_history.read_recent(days=30, limit=limit)
    except Exception as exc:  # noqa: BLE001 - a dashboard must not die on data
        logger.debug("economic history unavailable: %s", exc)
        return []


def economic_snapshot() -> Dict[str, Any]:
    """The latest economic run payload plus its config (for the Economic page)."""
    hist = economic_history(limit=1)
    latest = hist[-1] if hist else None
    return {
        "latest": latest,
        "config": load_macro_config(),
        "has_history": latest is not None,
    }


def band_from_inputs(cfg: Dict[str, Any], vwap: float) -> Dict[str, float]:
    """Recompute the POV band from the same inputs the slicer uses.

    Mirrors ``blue_engine.core.band`` exactly (ceiling = +x%, floor = -y%)
    so the Terminal page can draw the band even when no live engine is
    attached.
    """
    from blue_engine.core.band import compute_ceiling, compute_floor

    pov = _pov_block(cfg)
    ceiling_pct = float(pov.get("vwap_ceiling_pct", 0.5))
    floor_pct = float(pov.get("vwap_floor_pct", 3.0))
    if not vwap or vwap <= 0:
        return {"ceiling": 0.0, "floor": 0.0}
    return {
        "ceiling": compute_ceiling(vwap, ceiling_pct),
        "floor": compute_floor(vwap, floor_pct),
        "ceiling_pct": ceiling_pct,
        "floor_pct": floor_pct,
    }


# ---------------------------------------------------------------------------
# Portfolio analytics (Phases 1-5) - read-only helpers for the Portfolio page
# ---------------------------------------------------------------------------
#
# These mirror ``economic_snapshot()`` above: one function per panel, each
# best-effort so a data fault blanks a panel instead of killing the page.
#
# They go through ``PortfolioManager`` (the stable public API) rather than
# touching the engine, so the dashboard is insulated from engine internals and
# a future manager implementation needs no page changes.

#: The manager is stateful (it holds the equity tracker), so we cache ONE
#: instance per process. Rebuilding it per poll would reset the tracker and
#: re-book every fill on every refresh.
_MANAGER: Optional[Any] = None


def portfolio_manager() -> Optional[Any]:
    """The shared :class:`PortfolioManager` instance, or None when unavailable.

    Cached deliberately: the manager owns the equity tracker, and a fresh
    instance per dashboard poll would forget every ingested fill and re-book it
    on the next tick — double-counting P&L.
    """
    global _MANAGER
    if _MANAGER is not None:
        return _MANAGER
    try:
        from blue_engine.portfolio import portfolio_manager as pm_mod
        # persist=False: the dashboard is a READER. Persisting from a UI poll
        # would write a portfolio run every 5 seconds and bury the real daily
        # history in noise.
        _MANAGER = pm_mod.PortfolioManager.create({"persist": False})
        return _MANAGER
    except Exception as exc:  # noqa: BLE001 - a dashboard must not die on data
        logger.debug("portfolio manager unavailable: %s", exc)
        return None


def equity_curve(months: int = 0) -> Dict[str, Any]:
    """Phase 1: the stored NAV path and its daily returns (for the curve chart).

    ``months=0`` (the default) means **the whole stored history**, so the curve
    always includes its inception row and the chart starts where capital was
    committed rather than mid-flight.
    """
    pm = portfolio_manager()
    if pm is None:
        return {"ok": False, "rows": [], "nav": [], "dates": [],
                "returns": [], "warnings": ["portfolio manager unavailable"]}
    try:
        return pm.get_equity_curve(months=months)
    except Exception as exc:  # noqa: BLE001
        logger.debug("equity curve unavailable: %s", exc)
        return {"ok": False, "rows": [], "nav": [], "dates": [],
                "returns": [], "warnings": [f"equity curve unavailable: {exc}"]}


def risk_report(months: int = 0) -> Dict[str, Any]:
    """Phase 2: the risk & performance report (Risk / Efficiency panels).

    ``months=0`` (the default) means **the whole stored history**. That is the
    right default for a report: a clipped window silently drops the inception row
    and the curve then starts mid-flight, so total return, drawdown and
    volatility are all measured from the wrong baseline.
    """
    pm = portfolio_manager()
    if pm is None:
        return {"ok": False, "performance": {}, "risk": {},
                "warnings": ["portfolio manager unavailable"]}
    try:
        return pm.get_risk_report(months=months)
    except Exception as exc:  # noqa: BLE001
        logger.debug("risk report unavailable: %s", exc)
        return {"ok": False, "performance": {}, "risk": {},
                "warnings": [f"risk report unavailable: {exc}"]}


def exposure_snapshot() -> Dict[str, Any]:
    """Phase 3: sector / long-short / beta / concentration for the Exposure tab."""
    pm = portfolio_manager()
    if pm is None:
        return {"ok": False, "by_sector": {}, "long_short": {}, "beta": {},
                "concentration": {}, "warnings": ["portfolio manager unavailable"]}
    try:
        return pm.get_exposure()
    except Exception as exc:  # noqa: BLE001
        logger.debug("exposure unavailable: %s", exc)
        return {"ok": False, "by_sector": {}, "long_short": {}, "beta": {},
                "concentration": {},
                "warnings": [f"exposure unavailable: {exc}"]}


def attribution_snapshot() -> Dict[str, Any]:
    """Phase 3: P&L by sleeve / session bucket / strategy."""
    pm = portfolio_manager()
    if pm is None:
        return {"ok": False, "by_sleeve": {}, "by_bucket": {},
                "by_strategy": {}, "warnings": ["portfolio manager unavailable"]}
    try:
        return pm.get_attribution()
    except Exception as exc:  # noqa: BLE001
        logger.debug("attribution unavailable: %s", exc)
        return {"ok": False, "by_sleeve": {}, "by_bucket": {},
                "by_strategy": {},
                "warnings": [f"attribution unavailable: {exc}"]}


def policy_snapshot() -> Dict[str, Any]:
    """Phase 4: the current optimiser decision (incl. kill-switch state)."""
    pm = portfolio_manager()
    if pm is None:
        return {"ok": False, "action": "UNKNOWN", "trade": None,
                "warnings": ["portfolio manager unavailable"]}
    try:
        return pm.get_policy()
    except Exception as exc:  # noqa: BLE001
        logger.debug("policy unavailable: %s", exc)
        return {"ok": False, "action": "UNKNOWN", "trade": None,
                "warnings": [f"policy unavailable: {exc}"]}


def views_snapshot() -> Dict[str, Any]:
    """Phase 5: stated-vs-allowed view confidences (the Views panel).

    Reads the summary only — never triggers an optimiser run, because a UI poll
    must not be able to move the book.
    """
    pm = portfolio_manager()
    if pm is None:
        return {"ok": False, "views": [], "n_in": 0, "n_out": 0,
                "warnings": ["portfolio manager unavailable"]}
    try:
        problem = pm.get_view_problem()
        return problem.get("view_summary") or {
            "ok": False, "views": [], "n_in": 0, "n_out": 0,
            "warnings": ["no view summary available"]}
    except Exception as exc:  # noqa: BLE001
        logger.debug("views unavailable: %s", exc)
        return {"ok": False, "views": [], "n_in": 0, "n_out": 0,
                "warnings": [f"views unavailable: {exc}"]}


# ---------------------------------------------------------------------------
# ML training (offline learning layer) - read-only helpers for the ML page
# ---------------------------------------------------------------------------
#
# Same contract as the economic/portfolio helpers above: ONE function per
# panel, each best-effort, so a missing artifact blanks a panel instead of
# killing the page.
#
# READ-ONLY, and deliberately so. This module never calls ``run_training``:
# a UI poll must never start a training run. It reads the registry index that
# the training orchestrator already wrote (``data/ml_artifacts/``), which keeps
# the dashboard's cost at one file read per poll and means an operator can
# leave the page open without a model fitting in the background.


def ml_registry_history(limit: int = 100) -> List[Dict[str, Any]]:
    """The artifact index (oldest first). Empty list when nothing trained yet.

    Backed by ``blue_engine.ml.registry.Registry.history``. Best-effort: any
    import or IO failure yields [] so the page shows "no artifacts" rather
    than an error box. The registry itself already tolerates a torn last line
    (a killed process mid-append), so this is safe on a flaky disk.
    """
    try:
        from blue_engine.ml.registry import Registry
        return Registry().history(limit=limit)
    except Exception as exc:  # noqa: BLE001 - a dashboard must not die on data
        logger.debug("ml registry history unavailable: %s", exc)
        return []


def ml_artifact(address: str) -> Optional[Dict[str, Any]]:
    """Load one artifact's record (payload + manifest), or None.

    ``require_status=None`` on purpose: the dashboard shows DRAFT artifacts
    too — an operator needs to see the candidate that FAILED the gates, not
    just the one that passed. The live-trading gate is the registry's
    ``load(require_status=validated)`` default, which live code uses and this
    page does not.
    """
    if not address:
        return None
    try:
        from blue_engine.ml.registry import Registry
        return Registry().load(address, require_status=None)
    except Exception as exc:  # noqa: BLE001
        logger.debug("ml artifact %s unreadable: %s", str(address)[:12], exc)
        return None


def ml_training_snapshot(limit: int = 50) -> Dict[str, Any]:
    """Everything the ML page renders: the runs, their fold metrics, verdicts.

    Shape (all keys always present, so the page never has to guard):

        {
          "ok":            bool,          # did we find any artifacts?
          "n_runs":        int,
          "runs":          [run, ...],    # NEWEST FIRST (index order reversed)
          "latest":        run | None,
          "candidates":    [cand, ...],   # flattened from the latest run
          "errors":        [str, ...],    # unreadable artifacts, shown not hidden
          "warnings":      [str, ...],
        }

    Each ``run`` carries the flattened provenance an operator actually checks:
    symbol, when, status, git commit, row count, and per-candidate results.

    Each ``cand`` is the walk-forward report flattened for display:
    name, family, verdict, skill mean/std, deflated-Sharpe prob_real, the cost
    gate, and the per-fold rows. Nothing is recomputed here — every number is
    exactly what the walk-forward runner reported at training time. Recomputing
    in the UI would let the page and the manifest disagree, which is the one
    thing a provenance display must never do.
    """
    hist = ml_registry_history(limit=limit)
    out: Dict[str, Any] = {"ok": bool(hist), "n_runs": 0, "runs": [],
                           "latest": None, "candidates": [],
                           "errors": [], "warnings": []}
    if not hist:
        out["warnings"].append(
            "no ML artifacts found — run the trainer to produce one")
        return out

    # The index is append-only with one line per SAVE and per PROMOTE, so the
    # same address appears more than once. De-duplicate keeping the LAST
    # occurrence (the newest status), or a promoted artifact would still show
    # as draft and the operator would think the promotion failed.
    by_address: Dict[str, Dict[str, Any]] = {}
    for rec in hist:
        addr = rec.get("address")
        if addr:
            by_address[addr] = rec
    records = list(by_address.values())

    for rec in reversed(records):          # newest first for display
        addr = rec.get("address", "")
        full = ml_artifact(addr)
        if full is None:
            out["errors"].append(f"artifact {str(addr)[:12]} unreadable")
            continue
        manifest = full.get("manifest") or {}
        payload = full.get("payload") or {}
        reports = manifest.get("reports") or {}

        out["runs"].append({
            "address": addr,
            "short_address": str(addr)[:12],
            "name": rec.get("name", "-"),
            "status": full.get("status", "draft"),
            "symbol": manifest.get("symbol", "-"),
            "run_id": manifest.get("run_id", "-"),
            "created_at": rec.get("created_at") or manifest.get("created_at"),
            "git_commit": manifest.get("git_commit") or "-",
            "ml_version": manifest.get("ml_version", "-"),
            "n_rows": manifest.get("n_rows"),
            "n_features": len(manifest.get("feature_names") or []),
            "horizon": manifest.get("horizon"),
            "n_splits": manifest.get("n_splits"),
            "n_trials": manifest.get("n_trials"),
            "sources": manifest.get("sources") or [],
            "best_candidate": payload.get("best_candidate"),
            "n_candidates": len(reports),
            "candidates": [_flatten_candidate(n, r)
                           for n, r in sorted(reports.items())],
        })

    out["n_runs"] = len(out["runs"])
    if out["runs"]:
        out["latest"] = out["runs"][0]
        out["candidates"] = out["latest"]["candidates"]
    return out


def _flatten_candidate(name: str, rep: Dict[str, Any]) -> Dict[str, Any]:
    """One candidate's walk-forward report, flattened for a table row.

    Kept as a pure function of the stored report (no recomputation) so the
    panel and the manifest can never disagree.
    """
    summary = rep.get("summary") or {}
    sk = summary.get("skill") or {}
    aucs = summary.get("auc") or {}
    deflation = rep.get("deflation") or {}
    cost = rep.get("cost_check") or {}
    spec = rep.get("spec") or {}
    return {
        "name": name,
        "family": spec.get("family") or summary.get("family") or "-",
        "ok": bool(rep.get("n_folds") or 0),
        "verdict": rep.get("verdict") or "NO VERDICT",
        "n_folds": rep.get("n_folds"),
        "n_errors": rep.get("n_errors"),
        # skill is the number that decides everything else (see metrics.skill)
        "skill_mean": sk.get("mean"),
        "skill_std": sk.get("std"),
        "skill_min": sk.get("min"),
        "auc_mean": aucs.get("mean"),
        # deflated Sharpe: the probability the result is not selection luck
        "prob_real": deflation.get("prob_real"),
        "deflated_sharpe": deflation.get("deflated_sharpe"),
        "expected_max_sharpe": deflation.get("expected_max_sharpe"),
        "n_trials": deflation.get("n_trials"),
        "deflation_verdict": deflation.get("verdict"),
        # the Indian-cost gate
        "gross_bps": cost.get("gross_bps"),
        "cost_bps": cost.get("cost_bps"),
        "net_bps": cost.get("net_bps"),
        "survives_cost": cost.get("survives"),
        "cost_verdict": cost.get("verdict"),
        "wall_seconds": rep.get("wall_seconds"),
        "folds": _flatten_folds(rep.get("folds") or []),
    }


def _flatten_folds(folds: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Per-fold rows for the drill-down table, with a display-ready date pair."""
    rows: List[Dict[str, Any]] = []
    for f in folds:
        tr = f.get("train_end_date") or "-"
        ts = f.get("test_start_date") or "-"
        rows.append({
            "fold": f.get("fold"),
            "train_rows": f.get("train_rows"),
            "test_rows": f.get("test_rows"),
            "train_end": tr,
            "test_start": ts,
            "window": f"< {tr}  ->  {ts} >",
            "accuracy": f.get("accuracy"),
            "base_rate": f.get("base_rate"),
            "skill": f.get("skill"),
            "auc": f.get("auc"),
            "sharpe": f.get("sharpe"),
            "error": f.get("error"),
        })
    return rows


__all__ = [
    "InputRow",
    "Snapshot",
    "attribution_snapshot",
    "band_from_inputs",
    "collect_inputs",
    "config_path",
    "economic_history",
    "economic_snapshot",
    "equity_curve",
    "exposure_snapshot",
    "fmt",
    "gather",
    "group_for_key",
    "load_config",
    "load_macro_config",
    "macro_config_path",
    "ml_artifact",
    "ml_registry_history",
    "ml_training_snapshot",
    "optimizer_snapshot",
    "policy_snapshot",
    "portfolio_manager",
    "risk_report",
    "views_snapshot",
]
# --------------------------------------------------------------------------- #
# Optimizer studies
# --------------------------------------------------------------------------- #


def _optimizer_study_paths() -> List[Path]:
    """Every study journal on disk, newest first. Never raises."""
    try:
        from blue_engine.optimizer import config as _cfg
        root = Path(_cfg.STUDIES_DIR)
    except Exception:  # noqa: BLE001
        return []
    if not root.is_dir():
        return []
    try:
        files = [p for p in root.glob("*.jsonl") if p.is_file()]
    except Exception:  # noqa: BLE001
        return []
    return sorted(files, key=lambda p: p.stat().st_mtime, reverse=True)


def optimizer_snapshot(name: Optional[str] = None, limit: int = 40
                       ) -> Dict[str, Any]:
    """Everything the Optimizer page renders, read from the study journals.

    WHY THIS READS THE JOURNAL RATHER THAN RE-RUNNING ANYTHING
    ---------------------------------------------------------
    A study costs real time, and its result is a *measurement* - if the page
    recomputed it, the page and the report could disagree, which is the one
    thing a provenance display must never do. So every number here is exactly
    what the engine recorded at run time.

    Shape (all keys always present, so the page never has to guard):

        {
          "ok":         bool,          # did we find any study?
          "studies":    [name, ...],   # newest first
          "name":       str | None,
          "n_trials":   int,
          "n_ok":       int,
          "n_failed":   int,
          "verdict":    dict,          # as recorded, or {}
          "importance": dict,          # {"importance":{}, "ranking":[], ...}
          "top_k":      [str, ...],
          "recommended": dict,
          "baseline":   dict,
          "trials":     [trial, ...],  # full records, for the plots
          "workflow":   dict,          # importance/slice/parallel data
          "report":     dict | None,   # the JSON report, when it exists
          "errors":     [str, ...],
          "warnings":   [str, ...],
        }

    Degrades to ``ok=False`` with a warning - it never raises, because a page
    that crashes on a malformed study is worse than one that says so.
    """
    out: Dict[str, Any] = {
        "ok": False, "studies": [], "name": None, "n_trials": 0, "n_ok": 0,
        "n_failed": 0, "verdict": {}, "importance": {}, "top_k": [],
        "recommended": {}, "baseline": {}, "trials": [], "workflow": {},
        "report": None, "errors": [], "warnings": [],
        # AUDIT FINDING D2 (2026-10-01). The per-trial cost-gate (gross_bps,
        # cost_bps, net_bps, verdict) is mirrored into ``study.meta`` after
        # the F2 handoff; the Markdown report surfaces it under
        # "Cost gate (best trial)". The dashboard must show the same number,
        # or the page and the report disagree. Default to None so the
        # verdict panel renders "-" when there is no gate to display.
        "cost_gate_best": None,
    }

    paths = _optimizer_study_paths()
    out["studies"] = [p.stem for p in paths]
    if not paths:
        out["warnings"].append(
            "no optimizer studies found - run Scripts/optimize_strategy.py")
        return out

    target = None
    if name:
        target = next((p for p in paths if p.stem == name), None)
        if target is None:
            out["warnings"].append(
                f"study {name!r} not found; showing the newest instead")
    target = target or paths[0]
    out["name"] = target.stem

    # --- replay the journal ------------------------------------------------ #
    try:
        from blue_engine.optimizer.store import load_study_jsonl
        rows = load_study_jsonl(target.stem)
    except Exception as exc:  # noqa: BLE001
        out["errors"].append(f"could not read {target.name}: {exc}")
        return out
    if not rows:
        out["errors"].append(f"{target.name} is empty or unreadable")
        return out

    out["n_trials"] = len(rows)
    out["n_ok"] = sum(1 for r in rows if r.get("ok"))
    out["n_failed"] = out["n_trials"] - out["n_ok"]

    # --- rebuild a Study so we can reuse the analysis module --------------- #
    # The workflow maths (importance, plateau detection, parallel coordinates)
    # lives in the optimizer package and is already tested. Re-implementing it
    # for the page would create a second source of truth for the same numbers.
    try:
        from blue_engine.optimizer.analysis import visual_workflow
        from blue_engine.optimizer.study import Study, TrialRecord

        meta = _read_study_meta(target.stem)
        study = Study(name=target.stem,
                      space_name=meta.get("space_name", ""),
                      strategy=meta.get("strategy", ""),
                      objective_version=meta.get("objective_version", "unknown"),
                      seed=int(meta.get("seed") or 0))
        for r in rows:
            study.add(TrialRecord(
                number=int(r.get("number", 0)),
                params=dict(r.get("params") or {}),
                value=r.get("value"), ok=bool(r.get("ok")),
                reason=r.get("reason"),
                mean_sharpe=r.get("mean_sharpe"),
                std_sharpe=r.get("std_sharpe"),
                min_sharpe=r.get("min_sharpe"),
                n_folds=int(r.get("n_folds") or 0),
                n_obs=int(r.get("n_obs") or 0),
                seconds=float(r.get("seconds") or 0.0),
                cached=bool(r.get("cached")),
                # AUDIT FINDING F2. The cost gate is round-tripped through
                # the journal so the dashboard can show the same numbers the
                # report does. Missing on older journals -> None, harmless.
                cost_gate=(dict(r["cost_gate"])
                           if isinstance(r.get("cost_gate"), dict) else None)))

        out["trials"] = rows
        out["verdict"] = study.verdict()
        out["importance"] = study.param_importance()
        # AUDIT FINDING D2 (2026-10-01). Surface the best trial's cost gate
        # at the top level so the verdict panel can render a "Cost gate"
        # row. The Markdown report already has this; the dashboard now
        # matches. ``_best_cost_gate`` is wrapped in try/except because a
        # missing or malformed gate (old journal, no scoreable trial) must
        # not bring the page down - degrade to None and the panel shows
        # "-".
        try:
            from blue_engine.optimizer.report import _best_cost_gate
            out["cost_gate_best"] = _best_cost_gate(study)
        except Exception:  # noqa: BLE001
            out["cost_gate_best"] = None
        wf = visual_workflow(study)
        out["workflow"] = wf
        out["top_k"] = wf.get("important_params") or []

        # Recommended params = the best trial's, snapped by the space when the
        # space is available. A missing space is not fatal - we then show the
        # raw winning values rather than nothing.
        best = study.best()
        if best and out["verdict"].get("ok"):
            try:
                sp = _load_space_for(meta.get("space_name"))
                out["recommended"] = sp.coerce(dict(best.params)) if sp \
                    else dict(best.params)
                out["baseline"] = sp.defaults() if sp else {}
            except Exception:  # noqa: BLE001
                out["recommended"] = dict(best.params)
        out["ok"] = True
    except Exception as exc:  # noqa: BLE001
        out["errors"].append(f"could not analyse {target.stem}: {exc}")
        return out

    # --- the report, when one was written ---------------------------------- #
    try:
        from blue_engine.optimizer import config as _cfg
        rp = Path(_cfg.REPORTS_DIR) / f"{target.stem}.json"
        if rp.is_file():
            out["report"] = json.loads(rp.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        out["warnings"].append(f"report unreadable: {exc}")

    return out


def _read_study_meta(name: str) -> Dict[str, Any]:
    """Read the sidecar meta written by the CLI, if present. Never raises."""
    try:
        from blue_engine.optimizer import config as _cfg
        mp = Path(_cfg.REPORTS_DIR) / f"{name}.json"
        if mp.is_file():
            blob = json.loads(mp.read_text(encoding="utf-8"))
            st = blob.get("study") or {}
            return {"space_name": st.get("space"),
                    "strategy": st.get("strategy"),
                    "objective_version": st.get("objective_version"),
                    "seed": st.get("seed")}
    except Exception:  # noqa: BLE001
        pass
    return {}


def optimizer_available_filters() -> Dict[str, Any]:
    """Read-only scan of every study on disk, used to populate the page's
    input header.

    Returns a dict shaped for three ``dcc.Dropdown`` controls:

        {
          "ok":           bool,                # are there any studies at all?
          "studies":      [name, ...],         # ALL study names, newest first
          "strategies":   [label, ...],        # unique space_name values,
                                               # one dropdown entry per space
          "symbols":      [label, ...],        # distinct "SYMBOL" tokens parsed
                                               # from study names when they
                                               # follow the convention
          "periods":      [label, ...],        # distinct "PERIOD" tokens parsed
                                               # the same way
        }

    The ``symbol`` and ``time period`` axes are CONVENTION-BASED. The optimizer
    itself does not record either; the CLI accepts a ``--name <...>`` flag
    and the operator is expected to follow the convention
    ``<SYMBOL>_<PERIOD>_<strategy>`` (e.g. ``NIFTY50_2024Q1_ocl_keylevel``)
    so the dashboard can group studies. Studies whose names do not parse
    still appear in ``studies`` - the dropdown falls back to a flat list of
    study names and the operator can pick by hand. This is the honest,
    non-fabricating behaviour the page requires.

    Never raises. Degrades to ``ok=False`` with empty lists on any failure.
    """
    out: Dict[str, Any] = {
        "ok": False,
        "studies": [],
        "strategies": [],
        "symbols": [],
        "periods": [],
    }
    try:
        paths = _optimizer_study_paths()
    except Exception:  # noqa: BLE001
        return out
    out["studies"] = [p.stem for p in paths]
    if not paths:
        return out

    strategies: set = set()
    symbols: set = set()
    periods: set = set()
    # Convention: study names shaped like ``<TOKEN>_<TOKEN>_<space>``. The
    # LAST token is the space_name (matches ``spaces.available_spaces()``).
    # The first two tokens, if they look like SYMBOL / PERIOD (uppercase,
    # no underscores inside), are surfaced as filter axes. Anything else
    # silently falls into the flat ``studies`` list.
    try:
        from blue_engine.optimizer.spaces import available_spaces
        known_spaces = set(available_spaces() or [])
    except Exception:  # noqa: BLE001
        known_spaces = set()
    import re as _re
    _tok = _re.compile(r"^[A-Z0-9]+$")
    for p in paths:
        stem = p.stem
        meta = _read_study_meta(stem)
        if meta.get("space_name"):
            strategies.add(str(meta["space_name"]))
        parts = stem.split("_")
        if len(parts) >= 3 and (not known_spaces or parts[-1] in known_spaces):
            head = parts[:-1]
            if len(head) >= 2 and _tok.match(head[0]) and _tok.match(head[1]):
                symbols.add(head[0])
                periods.add(head[1])
    out["strategies"] = sorted(strategies)
    out["symbols"] = sorted(symbols)
    out["periods"] = sorted(periods)
    out["ok"] = True
    return out


def _load_space_for(name: Optional[str]):
    """Load a search space by name, or None. Never raises."""
    if not name:
        return None
    try:
        from blue_engine.optimizer.spaces import load_space
        return load_space(str(name))
    except Exception:  # noqa: BLE001
        return None