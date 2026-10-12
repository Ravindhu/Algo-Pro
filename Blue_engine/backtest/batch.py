"""Cached, per-symbol optimization and out-of-sample validation workflow."""
from __future__ import annotations

import json
import logging
import math
import os
import pickle
import re
import tempfile
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from multiprocessing import get_context
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from .costs import CostModel, DEFAULT_MODEL
from .engine import BacktestConfig, run_backtest
from .strategy import as_optimizer_strategy
from ..optimizer import data_root as optimizer_data_root
from ..optimizer.data_cache import MAX_WINDOW_DAYS, OptimizerDataCache
from ..optimizer.engine import EngineConfig, OptimizerEngine
from ..optimizer.search_space import SearchSpace
from ..optimizer.strategies import resolve_strategy

logger = logging.getLogger("blue_engine.backtest.batch")

__all__ = [
    "BatchConfig", "BatchResult", "run_symbol_batch",
    "periods_per_year_for_interval",
]


def periods_per_year_for_interval(interval: str) -> float:
    """Annualization convention; intraday bars assume 375 trading minutes/day."""
    token = (interval or "").strip().lower()
    if token in ("1d", "1day", "day", "daily", "d"):
        return 252.0
    if token in ("1w", "1week", "week", "weekly", "w"):
        return 52.0
    if token in ("1mo", "1month", "month", "monthly", "m"):
        return 12.0
    match = re.fullmatch(r"(\d+)(m|min|h|hour)", token)
    if match:
        amount = int(match.group(1))
        unit = match.group(2)
        minutes = amount * (60 if unit in ("h", "hour") else 1)
        if minutes <= 0 or minutes > 375:
            raise ValueError(f"unsupported intraday interval {interval!r}")
        return 252.0 * 375.0 / minutes
    raise ValueError(
        f"cannot infer annualization for interval {interval!r}; "
        "use a daily, weekly, monthly, minute, or hour interval")


@dataclass(frozen=True)
class BatchConfig:
    """Controls data loading, IS search, OOS validation and output persistence."""

    exchange: str = "NSE"
    interval: str = "1d"
    start: str = ""
    end: str = ""
    oos_fraction: float = 0.2
    trials: int = 100
    folds: int = 4
    seed: int = 7
    cost_bps: float = 10.0
    starting_equity: float = 100000.0
    risk_free_per_period: float = 0.0
    periods_per_year: Optional[float] = None
    workers: Optional[int] = None
    download_workers: int = 4
    max_position_fraction: float = 1.0
    allow_short: bool = False

    def __post_init__(self) -> None:
        try:
            start_dt = date.fromisoformat(self.start)
            end_dt = date.fromisoformat(self.end)
        except ValueError as exc:
            raise ValueError("start and end must use YYYY-MM-DD") from exc
        if start_dt.isoformat() != self.start or end_dt.isoformat() != self.end:
            raise ValueError("start and end must use YYYY-MM-DD")
        if end_dt < start_dt:
            raise ValueError("end must be on or after start")
        if not 0.05 <= self.oos_fraction <= 0.5:
            raise ValueError("oos_fraction must be between 0.05 and 0.5")
        if self.trials < 1 or self.folds < 2:
            raise ValueError("trials must be >= 1 and folds must be >= 2")
        if self.download_workers < 1:
            raise ValueError("download_workers must be >= 1")
        if self.workers is not None and self.workers < 1:
            raise ValueError("workers must be >= 1")
        if not math.isfinite(self.starting_equity) or self.starting_equity <= 0:
            raise ValueError("starting_equity must be positive")
        if self.periods_per_year is not None and (
                not math.isfinite(self.periods_per_year)
                or self.periods_per_year <= 0):
            raise ValueError("periods_per_year must be finite and positive")
        if not math.isfinite(self.cost_bps):
            raise ValueError("cost_bps must be finite")
        if not math.isfinite(self.risk_free_per_period):
            raise ValueError("risk_free_per_period must be finite")
        if (not math.isfinite(self.max_position_fraction)
                or self.max_position_fraction <= 0
                or self.max_position_fraction > 1):
            raise ValueError("max_position_fraction must be in (0, 1]")


@dataclass
class BatchResult:
    """JSON-ready batch report and the approved-only parameter lookup."""

    timestamp: str
    interval: str
    configurations: Dict[str, Dict[str, Any]]
    results: Dict[str, Dict[str, Any]]
    output_path: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "timeframe": self.interval,
            "configurations": {
                symbol: {
                    "parameters": dict(item["parameters"]),
                    "oos_metrics": dict(item["oos_metrics"]),
                }
                for symbol, item in self.configurations.items()
            },
            "results": {symbol: dict(item)
                        for symbol, item in self.results.items()},
            "output_path": self.output_path,
        }


@dataclass(frozen=True)
class _SymbolRequest:
    symbol: str
    cache_root: str
    strategy_spec: Any
    search_space: Optional[SearchSpace]
    search_space_name: Optional[str]
    config: BatchConfig
    cost_model: CostModel
    run_id: str


def _cache_covers(status: Any, start: str, end: str) -> bool:
    if not status.cached or not status.first_ts or not status.last_ts:
        return False
    first = date.fromisoformat(str(status.first_ts)[:10])
    last = date.fromisoformat(str(status.last_ts)[:10])
    requested_start = date.fromisoformat(start)
    requested_end = date.fromisoformat(end)
    # Calendar date bounds need not contain bars on weekends or exchange
    # holidays; allow one non-trading week while still forcing stale caches to
    # refresh.
    return (first <= requested_start + timedelta(days=7)
            and last >= requested_end - timedelta(days=7))


def _download_symbol_window(cache: OptimizerDataCache, symbol: str,
                            config: BatchConfig) -> Dict[str, Any]:
    status = cache.status(exchange=config.exchange, symbol=symbol,
                          interval=config.interval)
    if _cache_covers(status, config.start, config.end):
        return {"ok": True, "symbol": symbol, "downloaded": False,
                "warnings": []}

    warnings: List[str] = []
    start_dt = date.fromisoformat(config.start)
    end_dt = date.fromisoformat(config.end)
    cursor = start_dt
    while cursor <= end_dt:
        chunk_end = min(cursor + timedelta(days=MAX_WINDOW_DAYS), end_dt)
        result = cache.download(
            exchange=config.exchange, symbol=symbol, interval=config.interval,
            start=cursor.isoformat(), end=chunk_end.isoformat())
        warnings.extend(result.warnings)
        if not result.ok:
            logger.error("data download failed for %s: %s", symbol, result.reason)
            return {"ok": False, "symbol": symbol,
                    "reason": result.reason or "data_download_failed",
                    "warnings": warnings}
        cursor = chunk_end + timedelta(days=1)

    status = cache.status(exchange=config.exchange, symbol=symbol,
                          interval=config.interval)
    if not _cache_covers(status, config.start, config.end):
        reason = (f"cache still does not cover requested window "
                  f"{config.start}..{config.end}")
        logger.error("%s: %s", symbol, reason)
        return {"ok": False, "symbol": symbol, "reason": reason,
                "warnings": warnings}
    return {"ok": True, "symbol": symbol, "downloaded": True,
            "warnings": warnings}


def _oos_gate(is_metrics: Dict[str, Any],
              oos_metrics: Dict[str, Any]) -> Dict[str, Any]:
    is_sharpe = is_metrics.get("sharpe")
    oos_sharpe = oos_metrics.get("sharpe")
    is_drawdown = is_metrics.get("max_drawdown_pct")
    oos_drawdown = oos_metrics.get("max_drawdown_pct")
    values = (is_sharpe, oos_sharpe, is_drawdown, oos_drawdown)
    if any(value is None or not math.isfinite(float(value)) for value in values):
        return {"approved": False, "reason": "required IS/OOS metrics are undefined"}
    if float(is_sharpe) <= 0:
        return {"approved": False, "reason": "IS Sharpe must be positive"}
    if float(is_drawdown) < 0 or float(oos_drawdown) < 0:
        return {"approved": False, "reason": "drawdown metrics must be non-negative"}
    sharpe_floor = 0.7 * float(is_sharpe)
    drawdown_ceiling = 1.3 * float(is_drawdown)
    sharpe_pass = float(oos_sharpe) >= sharpe_floor
    drawdown_pass = float(oos_drawdown) <= drawdown_ceiling
    return {
        "approved": sharpe_pass and drawdown_pass,
        "reason": None if sharpe_pass and drawdown_pass
                  else "OOS Sharpe or drawdown failed the configured gate",
        "sharpe_floor": sharpe_floor,
        "drawdown_ceiling_pct": drawdown_ceiling,
        "sharpe_pass": sharpe_pass,
        "drawdown_pass": drawdown_pass,
    }


def _run_symbol(request: _SymbolRequest) -> Dict[str, Any]:
    symbol = request.symbol
    state_history = ["RESEARCH"]
    try:
        cache = OptimizerDataCache(root=Path(request.cache_root))
        end_inclusive = f"{request.config.end} 23:59:59"
        loaded = cache.load(
            exchange=request.config.exchange, symbol=symbol,
            interval=request.config.interval, start=request.config.start,
            end=end_inclusive)
        if not loaded.ok or not loaded.bars:
            raise ValueError(
                f"cached data unavailable: {loaded.reason or 'no bars'}")
        bars = loaded.bars
        split_index = int(len(bars) * (1.0 - request.config.oos_fraction))
        minimum_is_bars = max(41, request.config.folds * 21)
        if split_index < minimum_is_bars or len(bars) - split_index < 21:
            raise ValueError(
                f"insufficient history: {len(bars)} bars; need at least "
                f"{minimum_is_bars} IS bars and 21 OOS bars")

        is_bars = bars[:split_index]
        oos_bars = bars[split_index:]
        search_name = f"batch_{request.run_id}_{symbol}"
        periods = (request.config.periods_per_year
                   or periods_per_year_for_interval(request.config.interval))
        search_cfg = EngineConfig(
            name=search_name, n_trials=request.config.trials,
            seed=request.config.seed, periods=len(is_bars),
            n_folds=request.config.folds, cost_bps=request.config.cost_bps,
            risk_free_per_period=request.config.risk_free_per_period,
            periods_per_year=periods)
        backtest_config = BacktestConfig(
            cost_model=request.cost_model,
            starting_equity=request.config.starting_equity,
            risk_free_per_period=request.config.risk_free_per_period,
            n_trials=1, periods_per_year=periods,
            max_position_fraction=request.config.max_position_fraction,
            allow_short=request.config.allow_short)
        signal, error = resolve_strategy(request.strategy_spec)
        if error or signal is None:
            raise ValueError(f"strategy resolution failed: {error}")
        adapter = as_optimizer_strategy(signal, config=backtest_config)
        search_space = request.search_space
        if search_space is None:
            from ..optimizer.spaces import load_space
            if not request.search_space_name:
                raise ValueError("worker request did not contain a search space")
            search_space = load_space(request.search_space_name)
        optimizer = OptimizerEngine(search_space, adapter, cfg=search_cfg)
        search = optimizer.search(bars=is_bars)
        params = search.recommended_params()
        if not search.ok or not params:
            verdict = search.verdict or {}
            raise ValueError(
                "IS search did not establish a recommendation: "
                f"{verdict.get('reason') or verdict.get('verdict') or 'no edge'}")

        state_history.append("VALIDATING")
        n_trials = max(1, len(search.study.trials))
        validation_config = BacktestConfig(
            cost_model=request.cost_model,
            starting_equity=request.config.starting_equity,
            risk_free_per_period=request.config.risk_free_per_period,
            n_trials=n_trials, periods_per_year=periods,
            max_position_fraction=request.config.max_position_fraction,
            allow_short=request.config.allow_short)
        is_result = run_backtest(
            is_bars, signal, params, config=validation_config,
            symbol=symbol, exchange=request.config.exchange,
            interval=request.config.interval)
        oos_result = run_backtest(
            oos_bars, signal, params, config=validation_config,
            symbol=symbol, exchange=request.config.exchange,
            interval=request.config.interval)
        gate = _oos_gate(is_result.metrics, oos_result.metrics)
        state = "APPROVED" if gate["approved"] else "DEGRADED"
        state_history.append(state)
        return {
            "symbol": symbol,
            "state": state,
            "state_history": state_history,
            "source": loaded.source,
            "data_class": loaded.data_class,
            "bars": len(bars),
            "is_window": [is_bars[0].get("ts"), is_bars[-1].get("ts")],
            "oos_window": [oos_bars[0].get("ts"), oos_bars[-1].get("ts")],
            "search_trials": n_trials,
            "best_parameters": dict(params),
            "is_metrics": dict(is_result.metrics),
            "oos_metrics": dict(oos_result.metrics),
            "validation": gate,
            "warnings": list(loaded.warnings),
        }
    except Exception as exc:
        logger.exception("symbol workflow failed for %s", symbol)
        return {
            "symbol": symbol, "state": "DEGRADED",
            "state_history": state_history + ["DEGRADED"],
            "reason": f"{type(exc).__name__}: {exc}",
        }


def _atomic_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path: Optional[Path] = None
    try:
        with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", newline="\n",
                dir=str(path.parent), prefix=f".{path.name}.",
                suffix=".tmp", delete=False) as stream:
            tmp_path = Path(stream.name)
            json.dump(payload, stream, indent=2, sort_keys=True, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(str(tmp_path), str(path))
    except Exception:
        if tmp_path is not None:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                logger.exception("could not remove incomplete output %s", tmp_path)
        raise


def run_symbol_batch(symbols: Sequence[str], *,
                     strategy: Any,
                     search_space: SearchSpace,
                     config: BatchConfig,
                     cache: Optional[OptimizerDataCache] = None,
                     cost_model: CostModel = DEFAULT_MODEL,
                     output_path: Optional[Path] = None) -> BatchResult:
    """Download/cache a symbol batch, optimize independently, then OOS-gate it.

    Historical bars are fully cached before any parameter search begins.
    Downloads use bounded threads; CPU-heavy symbol studies use independent
    processes by default (maximum four). Pass an importable strategy path or a
    module-level picklable callable when using more than one process. The
    approved lookup is persisted separately from live configuration.
    """
    normalized = list(dict.fromkeys(
        str(symbol).strip().upper() for symbol in symbols if str(symbol).strip()))
    if isinstance(symbols, (str, bytes)):
        raise TypeError("symbols must be a sequence of ticker strings, not one string")
    if not normalized:
        raise ValueError("symbols must contain at least one non-empty ticker")
    if not isinstance(search_space, SearchSpace):
        raise TypeError("search_space must be an optimizer SearchSpace")
    cache = cache or OptimizerDataCache()
    process_count = config.workers or min(
        len(normalized), max(1, min(os.cpu_count() or 1, 4)))
    process_count = min(process_count, len(normalized))
    worker_space = search_space
    worker_space_name: Optional[str] = None
    if process_count > 1:
        try:
            pickle.dumps((strategy, cost_model))
        except Exception as exc:
            raise TypeError(
                "parallel optimization requires a picklable strategy; "
                f"use 'module:function' or workers=1: {exc}") from exc
        try:
            pickle.dumps(search_space)
        except Exception as exc:
            from ..optimizer.spaces import SPACES
            if SPACES.get(search_space.name) is search_space:
                worker_space = None
                worker_space_name = search_space.name
            else:
                raise TypeError(
                    "parallel optimization requires a picklable custom "
                    "search space, or a registered built-in; use workers=1: "
                    f"{exc}") from exc

    timestamp = datetime.now(timezone.utc).isoformat(
        timespec="seconds").replace("+00:00", "Z")
    run_id = timestamp.replace(":", "").replace("-", "")
    downloads: Dict[str, Dict[str, Any]] = {}
    with ThreadPoolExecutor(
            max_workers=min(config.download_workers, len(normalized))) as pool:
        futures = {
            pool.submit(_download_symbol_window, cache, symbol, config): symbol
            for symbol in normalized
        }
        for future in as_completed(futures):
            symbol = futures[future]
            try:
                downloads[symbol] = future.result()
            except Exception as exc:
                logger.exception("cache preparation failed for %s", symbol)
                downloads[symbol] = {
                    "ok": False, "symbol": symbol,
                    "reason": f"{type(exc).__name__}: {exc}",
                }

    requests = {
        symbol: _SymbolRequest(
            symbol=symbol, cache_root=str(cache.root),
            strategy_spec=strategy, search_space=worker_space,
            search_space_name=worker_space_name,
            config=config, cost_model=cost_model, run_id=run_id)
        for symbol in normalized if downloads.get(symbol, {}).get("ok")
    }
    results: Dict[str, Dict[str, Any]] = {}
    for symbol in normalized:
        if symbol not in requests:
            results[symbol] = {
                "symbol": symbol, "state": "DEGRADED",
                "state_history": ["RESEARCH", "DEGRADED"],
                "reason": downloads.get(symbol, {}).get(
                    "reason", "cache preparation failed"),
                "warnings": downloads.get(symbol, {}).get("warnings", []),
            }

    if process_count == 1:
        for symbol, request in requests.items():
            results[symbol] = _run_symbol(request)
            results[symbol]["data_preparation"] = downloads[symbol]
    elif requests:
        with ProcessPoolExecutor(
                max_workers=min(process_count, len(requests)),
                mp_context=get_context("spawn")) as pool:
            futures = {pool.submit(_run_symbol, request): symbol
                       for symbol, request in requests.items()}
            for future in as_completed(futures):
                symbol = futures[future]
                try:
                    results[symbol] = future.result()
                    results[symbol]["data_preparation"] = downloads[symbol]
                except Exception as exc:
                    logger.exception("optimization worker failed for %s", symbol)
                    results[symbol] = {
                        "symbol": symbol, "state": "DEGRADED",
                        "state_history": ["RESEARCH", "DEGRADED"],
                        "data_preparation": downloads[symbol],
                        "reason": f"{type(exc).__name__}: {exc}",
                    }

    configurations = {
        symbol: {
            "parameters": dict(outcome["best_parameters"]),
            "oos_metrics": dict(outcome["oos_metrics"]),
        }
        for symbol, outcome in results.items()
        if outcome.get("state") == "APPROVED"
    }
    output_path = (Path(output_path) if output_path is not None else
                   Path(optimizer_data_root()) / "optimizer"
                   / "symbol_parameters.json")
    batch = BatchResult(
        timestamp=timestamp, interval=config.interval,
        configurations=configurations,
        results={symbol: results[symbol] for symbol in normalized},
        output_path=str(output_path))
    _atomic_json(output_path, batch.to_dict())
    logger.info(
        "symbol batch complete: %d symbols, %d OOS-approved, output=%s",
        len(normalized), len(configurations), output_path)
    return batch
