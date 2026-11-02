"""auto_tune.py - close the live feedback loop: measure -> re-optimize -> apply.

THE GAP THIS CLOSES
-------------------
Audit 2026-10-29. Every piece of the loop already existed and was never
connected:

    backtest/engine.py     deterministic walk-forward simulation
    optimizer/engine.py    TPE search + out-of-sample validation
    backtest/loops.py      LiveMonitor -> ReoptimizationSignal
    core/live_monitor.py   LiveMonitorGuard -> latches the KILL SWITCH

``LiveMonitorGuard._on_signal`` did exactly one thing when live performance
degraded: trip the durable kill switch. Nothing re-optimized, nothing produced
new parameters, and nothing could ever promote them. The loop was open - it
stopped trading but never improved.

This module is the missing conductor. On a triggered signal it:

    1. re-runs the batch optimizer on fresh historical data,
    2. applies the SAME approval policy the optimizer already enforces
       (OOS Sharpe floor, deflated-Sharpe probability, drawdown ratio),
    3. promotes approved parameters to the live config ONLY through a
       quarantine -> validate -> atomic swap path,
    4. records every attempt so the decision is auditable.

SAFETY MODEL (capital protection outranks performance)
------------------------------------------------------
* **Never promotes on a degraded run.** The kill switch has already latched by
  the time we get here; this module proposes parameters for the *next* session,
  it never un-halts anything. Un-halting stays an operator action.
* **Never trusts the optimizer.** ``approved`` is recomputed here from the
  batch report; a report missing its OOS statistics is rejected (fail closed).
* **Never writes a partial config.** The new strategy block is validated by
  round-tripping it through the real config parser first, then written with
  temp-file + ``os.replace`` so a crash cannot leave a truncated JSON.
* **Never promotes automatically in ``shadow`` mode** (the default): it computes
  and reports the change, and an operator applies it. ``apply`` must be chosen
  explicitly, because silently changing a live strategy is exactly the class of
  action this project's policy reserves for a human.
* **Bounded.** A cooldown prevents re-optimizing on every tick, and one
  optimization runs at a time per process.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

__all__ = [
    "AutoTuneConfig",
    "AutoTuner",
    "TuneResult",
    "approval_ok",
    "is_risk_key",
]

#: Name fragments that mark a key as a risk bound (audit 2026-11-03, fix 4).
#: Matching is by substring and is deliberately broad: over-blocking only stops
#: a promotion, which a human can still apply. The optimizer may never move
#: these keys, in either direction.
RISK_KEY_FRAGMENTS = (
    "max_", "risk", "kill", "circuit", "blocked", "require_", "allow",
    "daily_loss", "drawdown", "exposure", "live", "dry_run",
    "stop_loss", "stoploss", "trailing",
)


def is_risk_key(key: Any) -> bool:
    """True when ``key`` bounds risk and so is outside auto-tune's reach."""
    name = str(key).lower()
    return any(fragment in name for fragment in RISK_KEY_FRAGMENTS)


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


@dataclass
class AutoTuneConfig:
    """Tunables. Defaults are conservative and non-mutating."""

    #: ``shadow`` = compute and report only. ``apply`` = promote approved params.
    mode: str = "shadow"
    #: Minimum seconds between two optimization runs.
    cooldown_seconds: float = 1800.0
    #: Optimization window (walk-forward input).
    start: str = ""
    end: str = ""
    interval: str = "1d"
    trials: int = 60
    folds: int = 4
    oos_fraction: float = 0.2
    #: Approval policy (mirrors the optimizer's own defaults).
    min_oos_sharpe: float = 1.0
    oos_is_ratio: float = 0.7
    min_dsr_probability: float = 0.95
    max_dd_ratio: float = 1.3
    #: Where the applied parameters are recorded for audit.
    journal_path: Optional[str] = None

    @classmethod
    def from_dict(cls, raw: Optional[Dict[str, Any]]) -> "AutoTuneConfig":
        cfg = dict(raw or {})
        mode = str(cfg.get("mode", "shadow")).strip().lower()
        if mode not in ("shadow", "apply"):
            raise ValueError("auto_tune.mode must be 'shadow' or 'apply'")
        return cls(
            mode=mode,
            cooldown_seconds=float(cfg.get("cooldown_seconds", 1800.0)),
            start=str(cfg.get("start") or ""),
            end=str(cfg.get("end") or ""),
            interval=str(cfg.get("interval") or "1d"),
            trials=int(cfg.get("trials", 60)),
            folds=int(cfg.get("folds", 4)),
            oos_fraction=float(cfg.get("oos_fraction", 0.2)),
            min_oos_sharpe=float(cfg.get("min_oos_sharpe", 1.0)),
            oos_is_ratio=float(cfg.get("oos_is_ratio", 0.7)),
            min_dsr_probability=float(cfg.get("min_dsr_probability", 0.95)),
            max_dd_ratio=float(cfg.get("max_dd_ratio", 1.3)),
            journal_path=cfg.get("journal_path"),
        )


@dataclass
class TuneResult:
    """Outcome of one auto-tune attempt."""

    attempted: bool
    reason: str
    approved: bool = False
    parameters: Dict[str, Any] = field(default_factory=dict)
    promoted: bool = False
    #: Per-symbol rows from the batch report (approved and rejected alike).
    symbols: List[Dict[str, Any]] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    ts: str = field(default_factory=_now)

    def as_dict(self) -> Dict[str, Any]:
        return {"ts": self.ts, "attempted": self.attempted,
                "reason": self.reason, "approved": self.approved,
                "promoted": self.promoted, "parameters": self.parameters,
                "symbols": self.symbols, "errors": self.errors}


def _num(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number else None


def approval_ok(report: Dict[str, Any], *, min_oos_sharpe: float,
                oos_is_ratio: float, min_dsr_probability: float,
                max_dd_ratio: float) -> tuple[bool, str]:
    """Re-derive the optimizer's approval from the raw report.

    Returns ``(approved, reason)``. This deliberately does NOT trust a boolean
    in the report: the numbers are re-checked against the policy so a corrupted
    or partially-written report cannot promote itself.

    Missing statistics reject (fail closed) - an absent OOS number is not
    evidence of a good strategy.
    """
    if not isinstance(report, dict):
        return False, "report is not an object"
    if report.get("status") not in (None, "success", "ok"):
        return False, f"optimizer status is {report.get('status')!r}"
    rows = report.get("results")
    if not isinstance(rows, list) or not rows:
        return False, "optimizer returned no per-symbol results"

    approved_rows = 0
    for row in rows:
        if not isinstance(row, dict):
            return False, "malformed optimizer row"
        if not row.get("approved"):
            continue
        oos = _num(row.get("oos_sharpe"))
        is_sharpe = _num(row.get("is_sharpe"))
        dsr = _num(row.get("oos_dsr_probability", row.get("dsr_probability")))
        oos_dd = _num(row.get("oos_max_drawdown"))
        is_dd = _num(row.get("is_max_drawdown"))
        if oos is None or dsr is None:
            return False, (f"{row.get('symbol')}: missing out-of-sample "
                           "statistics")
        if oos < max(min_oos_sharpe, oos_is_ratio * (is_sharpe or 0.0)):
            return False, (f"{row.get('symbol')}: OOS Sharpe {oos:.2f} below "
                           "the approval floor")
        if dsr < min_dsr_probability:
            return False, (f"{row.get('symbol')}: deflated-Sharpe probability "
                           f"{dsr:.3f} below {min_dsr_probability}")
        # Drawdowns are reported as negative numbers (or positive magnitudes,
        # depending on the producer). Compare MAGNITUDES so the sign convention
        # cannot silently disable this check.
        oos_dd_mag = abs(oos_dd) if oos_dd is not None else None
        is_dd_mag = abs(is_dd) if is_dd is not None else None
        if oos_dd_mag and is_dd_mag and oos_dd_mag > max_dd_ratio * is_dd_mag:
            return False, (f"{row.get('symbol')}: OOS drawdown "
                           f"{oos_dd_mag:.2f} exceeds {max_dd_ratio}x IS "
                           f"{is_dd_mag:.2f}")
        approved_rows += 1

    if approved_rows == 0:
        return False, "no symbol passed out-of-sample validation"
    return True, f"{approved_rows} symbol(s) passed out-of-sample validation"


class AutoTuner:
    """Drives re-optimization and (optionally) promotes the result."""

    def __init__(self, config: AutoTuneConfig, *,
                 batch_runner: Optional[Callable[..., Any]] = None,
                 space_loader: Optional[Callable[[str], Any]] = None,
                 clock: Callable[[], float] = time.monotonic,
                 log: Optional[Callable[[str], None]] = None) -> None:
        self.config = config
        self._batch_runner = batch_runner
        self._space_loader = space_loader
        self._clock = clock
        self._log_sink = log
        self._last_run: Optional[float] = None
        self._lock = threading.Lock()
        self.history: List[Dict[str, Any]] = []

    # -- logging ---------------------------------------------------------
    def _log(self, message: str) -> None:
        line = f"[auto_tune {datetime.now().strftime('%H:%M:%S')}] {message}"
        if self._log_sink is not None:
            try:
                self._log_sink(line)
            except Exception:  # noqa: BLE001
                pass
        else:
            logger.info(line)

    # -- dependency resolution -------------------------------------------
    def _resolve_runner(self) -> Callable[..., Any]:
        if self._batch_runner is not None:
            return self._batch_runner
        from blue_engine.backtest.batch import run_symbol_batch
        return run_symbol_batch

    def _resolve_space(self, strategy: str) -> Any:
        if self._space_loader is not None:
            return self._space_loader(strategy)
        from blue_engine.optimizer.spaces import load_space
        return load_space(strategy)

    # -- cooldown ---------------------------------------------------------
    def within_cooldown(self) -> bool:
        if self._last_run is None:
            return False
        return (self._clock() - self._last_run) < self.config.cooldown_seconds

    # -- the loop ---------------------------------------------------------
    def on_signal(self, reasons: Any, *, symbol: str, strategy: str,
                  config_path: Optional[str] = None) -> TuneResult:
        """React to a ReoptimizationSignal. Never raises.

        Args:
            reasons: the monitor's reason tuple/str (recorded for the audit).
            symbol: the traded symbol to re-optimize.
            strategy: registered strategy name (e.g. ``ocl_keylevel``).
            config_path: live JSON whose strategy block may be updated.
        """
        reason_text = "; ".join(reasons) if isinstance(reasons, (list, tuple)) \
            else str(reasons or "unspecified")
        with self._lock:
            if self.within_cooldown():
                result = TuneResult(
                    attempted=False,
                    reason=f"cooldown active; last run {self._last_run}")
                self._record(result)
                return result
            self._last_run = self._clock()

        self._log(f"signal: {reason_text} -> re-optimizing {symbol}/{strategy}")
        try:
            report = self._run_optimization(symbol, strategy)
        except Exception as exc:  # noqa: BLE001 - never break the caller
            result = TuneResult(attempted=True, reason=reason_text,
                                errors=[repr(exc)])
            self._log(f"optimization failed: {exc!r}")
            self._record(result)
            return result

        result = self._evaluate(report, reason_text)
        if result.approved:
            parameters = self._parameters_for(report, symbol)
            result.parameters = parameters
            if self.config.mode == "apply" and config_path:
                promoted, error = self._promote(config_path, strategy, parameters)
                result.promoted = promoted
                if error:
                    result.errors.append(error)
            elif self.config.mode == "shadow":
                self._log("shadow mode: parameters computed but NOT applied")
        self._record(result)
        return result

    def _window(self) -> tuple[str, str]:
        """Resolve the optimization window, defaulting to a recent lookback.

        ``BatchConfig`` requires explicit ``YYYY-MM-DD`` bounds; leaving them
        empty raised on every run and the tuner silently reported "0 approved".
        """
        today = datetime.now().date()
        end = self.config.end or today.isoformat()
        if self.config.start:
            return self.config.start, end
        try:
            end_date = datetime.strptime(end, "%Y-%m-%d").date()
        except ValueError:
            end_date = today
        return (end_date - timedelta(days=730)).isoformat(), end

    def _run_optimization(self, symbol: str, strategy: str) -> Dict[str, Any]:
        runner = self._resolve_runner()
        space = self._resolve_space(strategy)
        from blue_engine.backtest.batch import BatchConfig

        start, end = self._window()
        batch_config = BatchConfig(
            start=start, end=end,
            interval=self.config.interval, trials=self.config.trials,
            folds=self.config.folds, oos_fraction=self.config.oos_fraction)
        output = runner([symbol], strategy=strategy, search_space=space,
                        config=batch_config)
        # The batch runner returns a result object or a dict depending on the
        # call site; normalise without guessing.
        if isinstance(output, dict):
            return output
        for attr in ("report", "as_dict", "to_dict"):
            value = getattr(output, attr, None)
            if callable(value):
                produced = value()
                if isinstance(produced, dict):
                    return produced
            elif isinstance(value, dict):
                return value
        raise TypeError(
            f"batch runner returned {type(output).__name__}; expected a "
            "report dict")

    def _evaluate(self, report: Dict[str, Any],
                  reason_text: str) -> TuneResult:
        approved, why = approval_ok(
            report, min_oos_sharpe=self.config.min_oos_sharpe,
            oos_is_ratio=self.config.oos_is_ratio,
            min_dsr_probability=self.config.min_dsr_probability,
            max_dd_ratio=self.config.max_dd_ratio)
        rows = report.get("results") if isinstance(report, dict) else []
        summary = [{
            "symbol": row.get("symbol"),
            "approved": bool(row.get("approved")),
            "oos_sharpe": row.get("oos_sharpe"),
            "is_sharpe": row.get("is_sharpe"),
            "trades": row.get("oos_trades", row.get("trades")),
        } for row in (rows or []) if isinstance(row, dict)]
        self._log(f"approval: {approved} ({why})")
        return TuneResult(attempted=True, reason=reason_text, approved=approved,
                          symbols=summary,
                          errors=[] if approved else [why])

    @staticmethod
    def _parameters_for(report: Dict[str, Any], symbol: str) -> Dict[str, Any]:
        configurations = report.get("configurations") or {}
        if isinstance(configurations, dict):
            params = configurations.get(symbol)
            if isinstance(params, dict):
                return dict(params)
        for row in report.get("results") or []:
            if isinstance(row, dict) and row.get("symbol") == symbol:
                params = row.get("best_parameters") or row.get("parameters")
                if isinstance(params, dict):
                    return dict(params)
        return {}

    # -- promotion ---------------------------------------------------------
    def _promote(self, config_path: str, strategy: str,
                 parameters: Dict[str, Any]) -> tuple[bool, str]:
        """Write approved parameters into the live JSON config, atomically."""
        if not parameters:
            return False, "no parameters to promote"
        path = Path(config_path)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            return False, f"cannot read live config: {exc!r}"
        if strategy not in raw or not isinstance(raw[strategy], dict):
            return False, f"live config has no '{strategy}' block"

        # Risk bounds are never tuned: loosening one would let a re-optimized
        # strategy trade bigger after a loss. The whole promotion is refused,
        # so no partial update is written.
        risk_keys = sorted(str(k) for k in parameters if is_risk_key(k))
        if risk_keys:
            return False, ("auto-tune may not change risk keys: "
                           + ", ".join(risk_keys))

        # Quarantine: build the candidate and prove the REAL parser accepts it
        # before anything on disk changes.
        candidate = json.loads(json.dumps(raw))
        candidate[strategy].update(parameters)
        try:
            from blue_engine.core.config_loader import parse_master_config
            parse_master_config(candidate)
        except Exception as exc:  # noqa: BLE001
            return False, f"candidate config rejected by the parser: {exc!r}"

        # The structural parser only checks SHAPE, not types: it happily
        # accepted ``quantity: "nonsense"`` in this module's own test. Validate
        # every promoted value against the type of the key it replaces, which
        # is the only schema the operator's config already agrees on.
        for key, value in parameters.items():
            if key not in raw[strategy]:
                # Refuse an UNKNOWN key outright. Without a declared type there
                # is nothing to validate against, and a typo (or an optimizer
                # adding `quantity` to a strategy that has no such setting)
                # would silently inject a dead key into the live config.
                return False, (f"'{key}' is not a setting of the '{strategy}' "
                               "block; refusing to add it")
            existing = raw[strategy].get(key)
            if existing is None:
                # Declared but currently null: allow any JSON scalar so an
                # operator can fill in an optional value.
                if not isinstance(value, (int, float, str, bool)):
                    return False, f"'{key}': unsupported new value {value!r}"
                continue
            if isinstance(existing, bool) or isinstance(value, bool):
                if not isinstance(value, bool):
                    return False, f"'{key}' must be a boolean, got {value!r}"
                continue
            if isinstance(existing, (int, float)):
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    return False, f"'{key}' must be numeric, got {value!r}"
            elif isinstance(existing, str) and not isinstance(value, str):
                return False, f"'{key}' must be text, got {value!r}"

        tmp: Optional[str] = None
        try:
            fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(candidate, fh, indent=2)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)
            tmp = None
        except Exception as exc:  # noqa: BLE001
            # Remove the orphaned temp file so failed writes leave no debris.
            if tmp is not None:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
            return False, f"atomic write failed: {exc!r}"
        self._log(f"promoted {len(parameters)} parameter(s) into {path.name}")
        return True, ""

    # -- audit -------------------------------------------------------------
    def _record(self, result: TuneResult) -> None:
        entry = result.as_dict()
        self.history.append(entry)
        path = self.config.journal_path
        if not path:
            return
        try:
            target = Path(path)
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, default=str) + "\n")
        except Exception as exc:  # noqa: BLE001
            logger.warning("auto_tune journal write failed: %s", exc)

    def report(self) -> Dict[str, Any]:
        return {"mode": self.config.mode, "runs": len(self.history),
                "history": list(self.history)}
