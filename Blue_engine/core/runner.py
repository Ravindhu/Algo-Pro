"""
runner.py - entry point for the blue_engine framework.

Usage
-----
    # Use the default master config (blue_engine/input/master.json)
    python -m blue_engine.core.runner

    # Use a specific config
    python -m blue_engine.core.runner blue_engine/input/my_master.json

The runner:
    1. Loads the master JSON config from blue_engine/input/
    2. Parses the strategies{} toggle block (only enabled strategies run)
    3. Initializes the OpenAlgo SDK client (env vars or JSON overrides)
    4. Subscribes to the order-update WebSocket feed (Feature 11)
       BEFORE the slicer runs, so partial / cancelled fills are
       folded into the durable ledger while the loop is active.
    5. Builds the slicer (POV) and any enabled signals
    6. Polls signals in round-robin until one fires its intent
    7. Runs the pre-trade RISK GATE (blue_engine.core.risk_gate). A breach of any
       configured limit (max_order_quantity / max_order_notional / max_daily_loss
       / max_open_exposure / blocked) aborts the run with exit code 4 before any
       order is sliced. With no "risk" block configured, the gate approves.
    8. Hands the approved intent to the slicer.execute(intent)

If no signal is enabled, the slicer runs immediately using the action and
quantity in the slicer's config block. This replaces the old "manual"
signal: just toggle all signals off and set the slicer's own action/qty.

The OpenAlgo mode (live vs analyze) is controlled exclusively in the
OpenAlgo UI. The runner only READS it via client.analyzerstatus() and
prints it for operator awareness. It never calls analyzertoggle().
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from blue_engine.alpha import signals
from blue_engine.pov import slicers
from blue_engine.core.config_loader import ConfigError, parse_master_config
from blue_engine.core.client import init_client
from blue_engine.core.mode import read_mode
from blue_engine.core.order_updates import subscribe_to_order_updates
from blue_engine.core.symbol_meta import is_sme_periodic_call_auction


# Default config used when the runner is called with no arguments.
DEFAULT_CONFIG = "blue_engine/input/master.json"


def load_raw_config(path: str) -> Dict[str, Any]:
    """Load and JSON-parse a config file from disk.

    Raises:
        FileNotFoundError, ValueError
    """
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(f"config not found: {path}")
    with p.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def _build_signal(name: str, params: Dict[str, Any], client):
    """Instantiate a signal class by its short name."""
    SignalCls = signals.get(name)
    return SignalCls(client, params)


def _build_slicer(name: str, params: Dict[str, Any], client):
    """Instantiate a slicer class by its short name."""
    SlicerCls = slicers.get(name)
    return SlicerCls(client, params)


def _annotate_sme_flag(client, symbol: str, exchange: str,
                       cfg: Dict[str, Any]) -> bool:
    """Probe the symbol master for SME/PCAS classification.

    The flag is appended to ``cfg["sme_periodic_call_auction"]`` so
    the slicer and the auction guards can read it without an extra
    SDK call. An explicit ``True``/``False`` already present in the
    JSON wins.
    """
    if "sme_periodic_call_auction" in cfg:
        return bool(cfg["sme_periodic_call_auction"])
    method = getattr(client, "symbol", None)
    if method is None:
        return False
    try:
        try:
            resp = method(symbol=symbol, exchange=exchange)
        except TypeError:
            resp = method(symbol, exchange)
    except Exception:
        return False
    if not isinstance(resp, dict) or resp.get("status") != "success":
        return False
    data = resp.get("data") or {}
    # Build a thin namespace the helper can read.
    class _Stub:
        pass
    stub = _Stub()
    for key in ("segment", "series", "instrumenttype", "symbol",
                "name", "brsymbol"):
        setattr(stub, key, data.get(key))
    flag = is_sme_periodic_call_auction(stub)
    if flag:
        cfg["sme_periodic_call_auction"] = True
    return flag


def _poll_signals_until_fire(
    signal_instances: List[Tuple[str, Any]],
    poll_seconds: float = 1.0,
    max_wait_seconds: float | None = None,
) -> Optional[Dict[str, Any]]:
    """Round-robin poll signals; return the first non-None intent.

    Returns None only if signal_instances is empty (caller should
    then build a manual intent from the slicer config).
    """
    if not signal_instances:
        return None
    import time
    deadline = (
        time.monotonic() + max_wait_seconds
        if max_wait_seconds and max_wait_seconds > 0
        else None
    )
    while True:
        for name, sig in signal_instances:
            intent = sig.detect()
            if intent is not None:
                print(f"[runner] signal '{name}' fired -> handing to slicer")
                return intent
        if deadline is not None and time.monotonic() >= deadline:
            print("[runner] signal poll budget elapsed with no intent; exiting")
            return None
        time.sleep(poll_seconds)


def run_with_config(raw: Dict[str, Any]) -> int:
    """Run the POV engine from an already-loaded raw config dict.

    This is the shared entry point used by both the JSON-file CLI
    (``main``) and external launchers such as ``run_pov.py`` that embed
    the user config directly in Python (no separate ``master.json`` needed).

    Returns 0 on success, non-zero on error.
    """
    try:
        cfg = parse_master_config(raw)
    except (ValueError, ConfigError) as exc:
        print(f"[runner] ERROR: {exc}")
        return 2

    active_signals = cfg["active_signals"]
    active_slicer = cfg["active_slicer"]

    enabled_names = [n for n, _ in active_signals]
    if active_slicer is not None:
        enabled_names.append(active_slicer[0])
    print(f"[runner] enabled strategies: {enabled_names or '<none>'}")

    # Initialise the OpenAlgo SDK client
    client = init_client(
        api_key=cfg.get("api_key"),
        host=cfg.get("host"),
        ws_url=cfg.get("ws_url"),
    )

    # Display (do NOT change) the current OpenAlgo mode
    mode = read_mode(client)
    print(f"[runner] OpenAlgo mode = {mode}  (controlled in OpenAlgo UI)")

    # Feature 11 - subscribe to the broker's order-update feed. The
    # subscription is global to the process and idempotent: only the
    # first run actually attaches; later runs reuse the bus.
    subscribed = subscribe_to_order_updates(client=client)
    if subscribed:
        print("[runner] order-update WebSocket subscription: active")
    else:
        print("[runner] order-update WebSocket subscription: degraded")

    # Feature X - subscribe to the broker's market-data WebSocket so the
    # POV slicer reads live volume (not the volume=0 REST fallback).
    from blue_engine.core.volume_cache import subscribe as subscribe_volume_cache
    vc_ok = subscribe_volume_cache(client=client)
    if vc_ok:
        print("[runner] volume-cache WebSocket: active")
    else:
        print("[runner] volume-cache WebSocket: degraded (volume will be 0)")

    # Feature 5 - probe the symbol master for SME/PCAS classification
    # so the auction guards and the runner log both agree.
    sme_detected = _annotate_sme_flag(
        client, cfg.get("symbol", ""), cfg.get("exchange", ""), cfg
    )
    if sme_detected:
        print(f"[runner] SME/PCAS detected for {cfg.get('symbol')}/"
              f"{cfg.get('exchange')}; auction-window guard active")

    # Build the slicer
    if active_slicer is None:
        print("[runner] no slicer enabled; nothing to do")
        return 0
    slicer_name, slicer_params = active_slicer
    try:
        slicer = _build_slicer(slicer_name, slicer_params, client)
    except (ValueError, KeyError) as exc:
        print(f"[runner] ERROR: could not build slicer '{slicer_name}': {exc}")
        return 3
    print(f"[runner] slicer: {slicer_name}  ({slicer.__class__.__name__})")

    # Build the signals
    signal_instances: List[Tuple[str, Any]] = []
    for name, params in active_signals:
        try:
            sig = _build_signal(name, params, client)
        except KeyError as exc:
            print(f"[runner] ERROR: {exc}")
            return 3
        signal_instances.append((name, sig))
        print(f"[runner] signal:  {name}  ({sig.__class__.__name__}) {sig.describe()}")

    # Either poll signals for an intent, or build a "manual"-style intent
    # directly from the slicer's own action/quantity.
    if signal_instances:
        intent = _poll_signals_until_fire(
            signal_instances,
            max_wait_seconds=cfg.get("signal_wait_seconds"),
        )
        if intent is None:
            print("[runner] no signal produced an intent; exiting")
            return 0
    else:
        # No signals: slicer fires immediately with its own action/qty.
        # This replaces the old "manual" signal.
        intent = {
            "action": slicer_params.get("action", "BUY"),
            "quantity": int(slicer_params.get("quantity",
                                              slicer_params.get("total_quantity", 0))),
            "tag": slicer_params.get("strategy_label", "POV"),
            "reason": "no signal enabled; slicer running immediately",
        }
        print(f"[runner] no signals -> running slicer immediately with "
              f"intent {intent}")

    # ---- Pre-trade risk gate (runs on the hot path, once, before the slicer).
    # The gate is fail-closed on a *breach* and fail-open when a limit is not
    # configured, so an operator who sets no limits keeps the old behaviour.
    # NOTE: read limits from the RAW config -- parse_master_config() normalises
    # to a whitelist of keys and drops the "risk" block, so cfg would lose it.
    # WIRE_PNL: day_pnl / open_exposure now come from the LIVE broker feed
    # (risk_context.fetch_risk_context -> client.positionbook()) unless the
    # operator set them explicitly in the config (config wins). This keeps the
    # max_daily_loss / max_open_exposure checks from silently skipping.
    from blue_engine.core.risk_gate import RiskGate
    from blue_engine.core.risk_context import fetch_risk_context

    # Normalise the operator override: an explicit top-level day_pnl/
    # open_exposure (or one inside the risk block) is treated as config truth.
    rc_cfg = dict(raw)
    rc_risk = rc_cfg.get("risk") if isinstance(rc_cfg.get("risk"), dict) else {}
    for _key in ("day_pnl", "open_exposure"):
        if rc_cfg.get(_key) is not None and rc_risk.get(_key) is None:
            rc_risk[_key] = rc_cfg.get(_key)
    rc_cfg["risk"] = rc_risk

    rc = fetch_risk_context(client, rc_cfg)
    for _warn in rc.get("warnings") or []:
        print(f"[runner] RISK CONTEXT warning: {_warn}")
    print(f"[runner] risk context: source={rc['source']} "
          f"day_pnl={rc['day_pnl']} open_exposure={rc['open_exposure']}")

    gate = RiskGate(raw, log=print)
    decision = gate.check(
        intent,
        day_pnl=rc.get("day_pnl"),
        open_exposure=rc.get("open_exposure"),
    )
    if not decision.approved:
        print(f"[runner] RISK GATE REJECTED intent: {decision.reason}")
        return 4
    print(f"[runner] risk gate passed: {decision.reason}")

    summary = slicer.execute(intent)
    print(f"[runner] summary: {summary}")
    return 0


def main(argv: list[str] | None = None) -> int:
    """CLI entry that loads a JSON config file and runs the engine.

    Returns 0 on success, non-zero on error.
    """
    argv = list(sys.argv[1:] if argv is None else argv)
    config_path = argv[0] if argv else DEFAULT_CONFIG

    print(f"[runner] loading config: {config_path}")
    try:
        raw = load_raw_config(config_path)
    except (FileNotFoundError, ValueError) as exc:
        print(f"[runner] ERROR: {exc}")
        return 2
    return run_with_config(raw)


if __name__ == "__main__":
    sys.exit(main())

