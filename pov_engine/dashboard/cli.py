"""
cli.py - command-line entry point for the OCL live dashboard.

Usage:

    # Run with default settings (NIFTY 50, short_term mode, paper).
    python -m pov_engine.dashboard.cli

    # Or pick a different universe.
    python -m pov_engine.dashboard.cli --preset NIFTY100

    # Or load full config from a JSON file.
    python -m pov_engine.dashboard.cli --config pov_engine/input/my_dashboard.json

In LIVE_TRADING=1 mode the dashboard will additionally call the
broker place-order API on every BUY/SELL intent. The default is
paper mode (no broker calls).

The CLI uses a simple line-based REPL after the Live refresh loop
exits (Ctrl+C). Commands:
    a <SYM> <EXCH>    add symbol to watchlist
    r <SYM> <EXCH>    remove symbol from watchlist
    s <SYM> <EXCH>    focus a symbol in the detail panel
    m investing|short_term  switch mode
    p                 print current P&L
    q                 quit
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="pov-dashboard",
        description="Live OCL Trading Dashboard (terminal UI)",
    )
    p.add_argument("--preset", default=None, help="Built-in watchlist preset (NIFTY50, NIFTY100, ...)")
    p.add_argument("--symbols", nargs="*", default=[], help="Extra SYM/EXCH pairs to add to the watchlist")
    p.add_argument("--exchange", default=None, help="Restrict watchlist to one exchange (e.g. NSE)")
    p.add_argument("--mode", choices=["investing", "short_term"], default=None)
    p.add_argument("--qty", type=int, default=None, help="qty_per_fire (investing mode)")
    p.add_argument("--risk", type=float, default=None, help="risk_pct (short_term mode)")
    p.add_argument("--max", type=int, default=None, help="max_symbols cap")
    p.add_argument("--quote-refresh", type=float, default=None, help="Quote refresh seconds")
    p.add_argument("--scan-refresh", type=float, default=None, help="Match scan refresh seconds")
    p.add_argument("--config", default=None, help="Path to a JSON config file (overrides flags)")
    p.add_argument("--paper", action="store_true", default=True, help="(default) Paper mode - no broker orders")
    p.add_argument("--live", action="store_true", help="LIVE trading mode - emits real broker orders (DANGEROUS)")
    p.add_argument("--log-level", default="INFO", help="Python log level")
    return p


def _load_config_from_file(path: str) -> Dict[str, Any]:
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(path)
    with p.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def _resolve_history_fn(live: bool):
    """Return a callable matching the OpenAlgo SDK client.history signature.

    The dashboard can run in two modes:
      - paper (default): no broker calls. We expose a fake history
        function so the candle prefetch is a no-op (the detail panel
        just shows "no candles" until the user wires the dashboard
        to a real broker).
      - live: wires the OpenAlgo SDK client. The user is responsible
        for installing the SDK and configuring OPENALGO_API_KEY,
        HOST_SERVER, and WEBSOCKET_URL.
    """
    if not live:
        def _fake_history(**kwargs):
            return {"status": "error", "message": "paper mode: history not available"}
        return _fake_history
    try:
        from openalgo import api
    except Exception as exc:
        raise RuntimeError(
            f"LIVE mode requires the openalgo SDK: {exc}. "
            "Install it with `pip install openalgo` or run with --paper (default)."
        ) from exc
    client = api(
        api_key=os.getenv("OPENALGO_API_KEY"),
        host=os.getenv("HOST_SERVER", "http://127.0.0.1:5000"),
        ws_url=os.getenv("WEBSOCKET_URL", "ws://127.0.0.1:8765"),
    )
    return client.history


def _resolve_quotes_fn(live: bool):
    if not live:
        def _fake_quotes(**kwargs):
            return {"status": "error", "message": "paper mode: quotes not available"}
        return _fake_quotes
    try:
        from openalgo import api
    except Exception as exc:
        raise RuntimeError(
            f"LIVE mode requires the openalgo SDK: {exc}. "
            "Install it with `pip install openalgo` or run with --paper (default)."
        ) from exc
    client = api(
        api_key=os.getenv("OPENALGO_API_KEY"),
        host=os.getenv("HOST_SERVER", "http://127.0.0.1:5000"),
        ws_url=os.getenv("WEBSOCKET_URL", "ws://127.0.0.1:8765"),
    )
    return client.quotes


def _build_config(args: argparse.Namespace) -> Any:
    # Late import: DashboardConfig is on the package.
    from pov_engine.dashboard.app import DashboardConfig

    raw: Dict[str, Any] = {}
    if args.config:
        raw = _load_config_from_file(args.config)

    def _get(key: str, default=None):
        return raw.get(key, default)

    extra_symbols = _get("extra_symbols") or []
    # --symbols SYM/EXCH pairs can also be added from the CLI.
    for spec in args.symbols or []:
        try:
            sym, exch = spec.split("/", 1)
            extra_symbols.append({"symbol": sym.strip().upper(), "exchange": exch.strip().upper()})
        except ValueError:
            pass

    return DashboardConfig(
        watchlist_preset=args.preset or _get("watchlist_preset", "NIFTY50"),
        extra_symbols=extra_symbols,
        exchange_filter=args.exchange or _get("exchange_filter"),
        max_symbols=args.max if args.max is not None else _get("max_symbols", 25),
        quote_refresh_seconds=args.quote_refresh if args.quote_refresh is not None else _get("quote_refresh_seconds", 3.0),
        scan_refresh_seconds=args.scan_refresh if args.scan_refresh is not None else _get("scan_refresh_seconds", 30.0),
        lookback_days=_get("lookback_days", 7),
        starting_cash=_get("starting_cash", 100000.0),
        mode=args.mode or _get("mode", "short_term"),
        qty_per_fire=args.qty if args.qty is not None else _get("qty_per_fire", 100),
        risk_pct=args.risk if args.risk is not None else _get("risk_pct", 0.01),
        touch_buffer_pct=_get("touch_buffer_pct", 0.05),
        ceiling_pct_above_trigger=_get("ceiling_pct_above_trigger", 1.0),
        floor_pct_below_trigger=_get("floor_pct_below_trigger", 1.0),
        stop_loss_pct_below_trigger=_get("stop_loss_pct_below_trigger", 2.0),
        pov_min_pct=_get("pov_min_pct", 5.0),
        pov_max_pct=_get("pov_max_pct", 25.0),
    )


async def _run_interactive(dash) -> None:
    """Run the dashboard until Ctrl+C, then a tiny REPL for symbol ops."""
    render_task = asyncio.create_task(dash.run())
    try:
        await render_task
    except asyncio.CancelledError:
        pass
    except KeyboardInterrupt:
        pass
    finally:
        dash.stop()


def main(argv: Optional[list] = None) -> int:
    args = _build_arg_parser().parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    live = bool(args.live and not args.paper)
    if live:
        logger.warning("LIVE TRADING MODE - real broker orders will be emitted")
    else:
        logger.info("paper mode - no broker orders (use --live to enable)")

    try:
        cfg = _build_config(args)
        history_fn = _resolve_history_fn(live)
        quotes_fn = _resolve_quotes_fn(live)
    except Exception as exc:
        logger.error("startup failed: %s", exc)
        return 1

    from pov_engine.dashboard.app import LiveDashboard

    dash = LiveDashboard(cfg, history_fn=history_fn, quotes_fn=quotes_fn)
    try:
        asyncio.run(_run_interactive(dash))
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())