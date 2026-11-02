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
    7. Runs the pre-trade RISK GATE (blue_engine.core.risk_gate), then rechecks
       every child order with a fresh quote and broker risk context before
       submission.
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
import math
import sys
import threading
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

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


def _is_paper_mode(cfg: Dict[str, Any]) -> bool:
    """True only when OpenAlgo is verifiably in analyze mode.

    Unreadable mode is NOT paper: the session guard must fail closed.
    """
    try:
        client = init_client(api_key=cfg.get("api_key"), host=cfg.get("host"),
                             ws_url=cfg.get("ws_url"))
        return str(read_mode(client)).strip().lower() == "analyze"
    except Exception:
        return False


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


def run_with_config(
    raw: Dict[str, Any], *,
    monitor_inputs_provider: Optional[Callable[..., Dict[str, Any]]] = None,
    paper: bool = False,
) -> int:
    """Run the POV engine from an already-loaded raw config dict.

    This is the shared entry point used by both the JSON-file CLI
    (``main``) and external launchers such as ``run_pov.py`` that embed
    the user config directly in Python (no separate ``master.json`` needed).

    ``monitor_inputs_provider`` may inject a trusted live telemetry provider
    for ``live_monitor``. Returns 0 on success, non-zero on error.
    """
    try:
        cfg = parse_master_config(raw)
    except (ValueError, ConfigError) as exc:
        print(f"[runner] ERROR: {exc}")
        return 2

    from blue_engine.portfolio.optim_policy import kill_switch_state
    kill_state = kill_switch_state()
    if kill_state["tripped"]:
        print("[runner] ERROR: durable kill switch is tripped; "
              "operator reset is required before starting")
        return 4

    active_signals = cfg["active_signals"]
    active_slicer = cfg["active_slicer"]

    enabled_names = [n for n, _ in active_signals]
    if active_slicer is not None:
        enabled_names.append(active_slicer[0])
    print(f"[runner] enabled strategies: {enabled_names or '<none>'}")

    # ---- Market session guard (audit 2026-10-29, LIVE-11) -----------------
    # There was no weekday/session check anywhere: a manual intent placed on a
    # Sunday or after 15:30 went to the broker and was simply rejected, and a
    # run started before 09:15 polled a closed book. This refuses NEW exposure
    # outside the session. It never blocks an exit (the exit engine is a
    # separate path), and analyze mode is exempt so paper runs work any time.
    from blue_engine.core.time_utils import market_session_state
    session_cfg = raw.get("market_session") or {}
    if not isinstance(session_cfg, dict):
        print("[runner] ERROR: market_session must be an object")
        return 2
    session = market_session_state(
        entry_cutoff=str(session_cfg.get("entry_cutoff", "14:45")))
    # Paper mode never touches a broker, so it may run at any hour: its purpose
    # is to validate the plumbing, and a closed market cannot make a real
    # order. An explicit ``market_session.enforce`` still wins for live runs,
    # and analyze mode is exempt for the same reason.
    exempt = paper or _is_paper_mode(cfg)
    if session_cfg.get("enforce", True):
        print(f"[runner] market session: {session['state']} - {session['reason']}")
        if session["state"] != "open" and not exempt:
            print(f"[runner] REFUSED: no new entries while the market is "
                  f"{session['state']} ({session['reason']}). Live trading "
                  f"happens only inside the continuous session.")
            return 6
        if session["state"] != "open" and exempt:
            print("[runner] paper/analyze run: session gate exempt "
                  "(no real order can be placed)")
    else:
        print("[runner] market session: NOT enforced (market_session.enforce=false)")

    # Initialise the OpenAlgo SDK client.
    #
    # AUDIT (2026-10-29): ``paper=True`` swaps the broker for a recording
    # PaperBroker, so the FULL live path (signal -> risk gate -> POV -> ledger
    # -> exit engine) runs without sending a single order to a real account.
    # Real quotes are used when a broker login exists; otherwise a deterministic
    # synthetic tape is used and the report says so.
    paper_broker = None
    if paper:
        from blue_engine.core.paper_broker import PaperBroker
        real = None
        try:
            real = init_client(api_key=cfg.get("api_key"), host=cfg.get("host"),
                               ws_url=cfg.get("ws_url"))
        except Exception as exc:  # noqa: BLE001 - synthetic tape is acceptable
            print(f"[runner] paper: real data unavailable ({exc}); "
                  f"using a deterministic synthetic tape")
        starting_cash = float(
            (raw.get("pov") or {}).get("starting_cash", 100000.0))
        paper_broker = PaperBroker(starting_cash=starting_cash,
                                   data_source=real)
        client = paper_broker
        print("[runner] PAPER MODE: orders are recorded, never sent to a broker")
    else:
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
        print("[runner] order-update WebSocket subscription: DEGRADED")

    # AUDIT (2026-10-29, LIVE-8): a live parent whose fills are never reported
    # cannot know what it owns. Without the push feed the ledger stays at 0, the
    # POV loop re-slices to its duration limit, and a later stop is sized from a
    # quantity that was never verified. Live means: a verified feed OR refuse.
    mode_for_feed = str(mode).strip().lower()
    if not subscribed and mode_for_feed != "analyze" and not paper:
        # Paper mode fills synchronously inside PaperBroker, so there is no
        # asynchronous fill to miss; a live/unknown run has no such guarantee.
        print("[runner] ERROR: order-update feed unavailable in LIVE mode. "
              "The engine cannot verify fills, so it will not place an order. "
              "Check OPENALGO_API_KEY / the websocket_url in the OpenAlgo UI.")
        return 5

    # Feature X - subscribe to the broker's market-data WebSocket so the
    # POV slicer reads live volume (not the volume=0 REST fallback).
    # AUDIT (2026-10-29, P0-5): the subscription must cover THIS run's symbol.
    # It used to stream a hardcoded INFY/TCS/RELIANCE list and fall back to
    # INFY's token, so any other symbol sliced on volume=0.
    from blue_engine.core.volume_cache import subscribe as subscribe_volume_cache
    run_symbol = (cfg.get("symbol", ""), cfg.get("exchange", ""))
    vc_ok = subscribe_volume_cache(client=client, symbols=[run_symbol])
    if vc_ok:
        print(f"[runner] volume-cache WebSocket: active for "
              f"{run_symbol[0]}/{run_symbol[1]}")
    else:
        print("[runner] volume-cache WebSocket: degraded (volume will be 0); "
              "POV will not slice on live volume")

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

    # Optional live feedback-loop guard. It is deliberately opt-in because a
    # valid monitor requires real, strategy-specific historical and live
    # telemetry; it must never invent data or present backtests as live.
    from blue_engine.backtest.loops import LiveMonitor
    from blue_engine.core.live_monitor import (
        LiveMonitorGuard, resolve_monitor_provider)
    monitor_guard = None
    monitor_cfg = raw.get("live_monitor")
    if monitor_cfg is not None and not isinstance(monitor_cfg, dict):
        print("[runner] ERROR: live_monitor must be an object")
        return 4
    if (isinstance(monitor_cfg, dict)
            and not isinstance(monitor_cfg.get("enabled", False), bool)):
        print("[runner] ERROR: live_monitor.enabled must be a boolean")
        return 4
    if isinstance(monitor_cfg, dict) and monitor_cfg.get("enabled", False):
        envelope = monitor_cfg.get("historical_envelope")
        if not isinstance(envelope, dict):
            print("[runner] ERROR: live_monitor requires historical_envelope")
            return 4
        baseline_source = monitor_cfg.get("baseline_source") or envelope.get("source")
        if baseline_source not in ("live", "verified_live"):
            print("[runner] ERROR: live_monitor baseline_source must be 'live' or 'verified_live'; backtest baselines are forbidden")
            return 4
        envelope = {**envelope, "source": baseline_source}
        provider = monitor_inputs_provider
        if provider is None:
            provider_ref = monitor_cfg.get("provider_ref")
            try:
                provider = resolve_monitor_provider(provider_ref)
            except (TypeError, ValueError) as exc:
                print(f"[runner] ERROR: live monitor provider: {exc}")
                return 4
        try:
            monitor = LiveMonitor(
                envelope,
                drawdown_multiple=float(
                    monitor_cfg.get("drawdown_multiple", 1.5)),
                loss_streak_multiple=float(
                    monitor_cfg.get("loss_streak_multiple", 1.4)),
                regime_std_multiple=float(
                    monitor_cfg.get("regime_std_multiple", 2.0)),
                regime_window=int(monitor_cfg.get("regime_window", 10)),
                baseline_source=baseline_source,
            )
            monitor_guard = LiveMonitorGuard(
                client=client, monitor=monitor, provider=provider,
                monitor_config=monitor_cfg)
        except (RuntimeError, TypeError, ValueError) as exc:
            print(f"[runner] ERROR: invalid live monitor configuration: {exc}")
            return 4
        # Auto-tune handoff: when live performance degrades, the same signal
        # that latches the kill switch now also drives a re-optimization.
        tune_cfg = raw.get("auto_tune")
        if isinstance(tune_cfg, dict) and tune_cfg.get("enabled", True):
            try:
                from blue_engine.core.auto_tune import AutoTuner, AutoTuneConfig
                monitor_guard.auto_tuner = AutoTuner(
                    AutoTuneConfig.from_dict(tune_cfg),
                    log=print)
                monitor_guard.symbol = cfg.get("symbol", "")
                monitor_guard.strategy = str(
                    raw.get("auto_tune_strategy")
                    or (active_signals[0][0] if active_signals else ""))
                monitor_guard.config_path = None  # runner takes a dict, not a path
                print(f"[runner] auto-tune: enabled ({monitor_guard.auto_tuner.config.mode} mode)")
            except Exception as exc:  # noqa: BLE001
                print(f"[runner] WARNING: auto_tune config rejected: {exc!r}")
        else:
            print("[runner] auto-tune: disabled")

        slicer.monitor_observer = monitor_guard.observe
        slicer.on_data_failure = monitor_guard.on_data_failure
        print("[runner] live performance monitor: active; observing valid "
              "quotes and checking each child order")
    else:
        print("[runner] live performance monitor: disabled")

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

    # ---- Pre-trade gate: intent check now, then a fresh check per child.
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
    requires_order_price = (
        gate.max_order_notional is not None
        or gate.max_open_exposure is not None)
    # A live run (OpenAlgo not in analyze mode) is flagged so the gate can
    # refuse an unconfigured/unlimited risk profile when require_live_ack is set.
    # read_mode() returns "live" | "analyze" | "unknown". "unknown" is treated
    # as live on purpose: an unverifiable mode must fail closed, never assume
    # that orders are harmless.
    is_live = str(mode).strip().lower() != "analyze"
    if is_live:
        # Fail closed: a live run must not start unless halts are durable.
        try:
            from blue_engine.core import kill_switch_store as _kss
            _kss.require_durable()
        except Exception as exc:  # noqa: BLE001 - refuse, never trade blind
            print(f"[runner] CRITICAL: {exc}; refusing live run")
            return 2

    def _valid_price(value: Any) -> Optional[float]:
        if isinstance(value, bool):
            return None
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return None
        return parsed if math.isfinite(parsed) and parsed > 0 else None

    def _authorize_child(*, phase: str, quantity: int, action: str,
                         price: Optional[float] = None) -> Optional[int]:
        if monitor_guard is not None:
            monitored_qty = monitor_guard.authorize_order(
                phase=phase, quantity=quantity, action=action, price=price)
            if monitored_qty is None or monitored_qty == 0:
                return monitored_qty
            quantity = min(quantity, monitored_qty)

        if requires_order_price and _valid_price(price) is None:
            print("[runner] RISK GATE rejected child: no fresh finite, "
                  "positive quote/limit price")
            return None
        child_intent = {
            "action": action,
            "quantity": quantity,
            "price": _valid_price(price),
        }
        child_context = fetch_risk_context(client, rc_cfg)
        for warning in child_context.get("warnings") or []:
            print(f"[runner] RISK CONTEXT warning: {warning}")
        child_decision = gate.check(
            child_intent,
            day_pnl=child_context.get("day_pnl"),
            open_exposure=child_context.get("open_exposure"),
            require_intent_price=requires_order_price,
            reduces_exposure=phase == "exiting",
            position_quantity=abs(held),
            live=is_live,
        )
        if not child_decision.approved:
            print(f"[runner] RISK GATE rejected child: {child_decision.reason}")
            return None
        return quantity

    slicer.pre_order_check = _authorize_child

    # Monetary limits require a live reference price even before the first
    # POV poll; the per-child check below refreshes it before every submission.
    if requires_order_price:
        try:
            quote = client.quotes(symbol=slicer.symbol, exchange=slicer.exchange)
        except Exception as exc:
            print(f"[runner] ERROR: unable to fetch pre-trade quote: {exc!r}")
            return 4
        quote_data = quote.get("data") if isinstance(quote, dict) else None
        if not isinstance(quote_data, dict):
            print("[runner] ERROR: pre-trade quote response is invalid")
            return 4
        quote_price = None
        for key in ("ltp", "last_price", "lastPrice", "lp", "close"):
            quote_price = _valid_price(quote_data.get(key))
            if quote_price is not None:
                break
        if (quote.get("status") != "success" or quote_price is None):
            print("[runner] ERROR: pre-trade quote is not successful with a "
                  "finite positive LTP")
            return 4
        intent["price"] = quote_price

    # Position-dependent checks are repeated at child submission when the
    # slicer's phase tells us whether this is an entry or an exit.
    # AUDIT (2026-10-22, P1-7): this call used ``reduces_exposure=True``
    # unconditionally, which switched off the max_daily_loss breach for a real
    # ENTRY. An approved intent here then only had to pass the per-child check,
    # so a risk-blocked entry was reported as "risk gate passed". The side is
    # derived from the intent/position instead.
    intent_action = str(intent.get("action", "")).upper()
    held = 0
    try:
        positions = client.positionbook() or {}
        for row in (positions.get("data") or []):
            if (str(row.get("symbol")) == str(cfg.get("symbol"))
                    and str(row.get("exchange")).upper() == str(cfg.get("exchange")).upper()):
                held = int(float(row.get("quantity") or 0))
    except Exception as exc:  # noqa: BLE001 - fail-closed below
        print(f"[runner] WARNING: position lookup failed ({exc!r}); "
              f"assuming this intent increases exposure")
        held = 0
    intent_reduces = ((intent_action == "SELL" and held > 0)
                      or (intent_action == "BUY" and held < 0))
    decision = gate.check(
        intent,
        day_pnl=rc.get("day_pnl"),
        open_exposure=rc.get("open_exposure"),
        require_intent_price=requires_order_price,
        reduces_exposure=intent_reduces,
        position_quantity=abs(held),
        live=is_live,
    )
    if not decision.approved:
        print(f"[runner] RISK GATE REJECTED intent: {decision.reason}")
        return 4
    print(f"[runner] risk gate passed: {decision.reason}")

    # Re-check the durable kill switch at the moment of execution: the switch
    # was read before the (possibly long) signal wait, and an operator trip or
    # a breach from another process during that wait must stop this run.
    from blue_engine.portfolio.optim_policy import peek_kill_switch
    if peek_kill_switch().get("tripped"):
        print("[runner] ERROR: kill switch tripped during the run; "
              "execution aborted")
        return 4
    slicer.kill_tripped = lambda: bool(peek_kill_switch().get("tripped"))

    summary = slicer.execute(intent)
    print(f"[runner] summary: {summary}")
    if monitor_guard is not None and monitor_guard.halted:
        print(f"[runner] live monitor halted new execution: "
              f"{monitor_guard.halt_reason}")
        return 4

    # ---- Broker-side protection (audit 2026-10-29, P0-2) ------------------
    # Without this the process returned 0 immediately after the entry and no
    # component watched the filled position. The exit engine places an SL-M
    # under the registry's level and keeps reconciling until the configured
    # window ends; a CRITICAL report trips the durable kill switch so no
    # later run can add to an unprotected book.
    exit_rc = _run_exit_engine(
        client, raw, int(summary.get("filled") or 0) if isinstance(
            summary, dict) else 0)
    if paper_broker is not None:
        _print_paper_report(paper_broker)
    return 4 if exit_rc else 0


def _print_paper_report(broker) -> None:
    """Reconcile a paper session: what the engine believed vs what it did."""
    try:
        report = broker.summary()
    except Exception as exc:  # noqa: BLE001
        print(f"[runner] paper report unavailable: {exc!r}")
        return
    print("[runner] ===== PAPER SESSION REPORT =====")
    print(f"[runner] data          : {report['data_classification']}"
          f"  (real calls={report['real_data_calls']})")
    print(f"[runner] orders        : {report['orders']} "
          f"(filled={report['filled_orders']}, "
          f"resting={report['resting_orders']}, "
          f"cancelled={report['cancelled_orders']})")
    print(f"[runner] traded notional: {report['traded_notional']:.2f}")
    print(f"[runner] cash          : {report['starting_cash']:.2f} -> "
          f"{report['ending_cash']:.2f}")
    for pos in report["open_positions"]:
        print(f"[runner] position      : {pos['symbol']} qty={pos['netqty']} "
              f"avg={pos['average_price']} pnl={pos['pnl']}")
    if report["data_classification"] == "synthetic":
        print("[runner] NOTE: synthetic prices - this validates the PLUMBING, "
              "not strategy performance")
    print("[runner] =============================")


def _run_exit_engine(client, raw: Dict[str, Any], filled: int) -> bool:
    """Protect the filled position. Returns True on a CRITICAL failure."""
    cfg = raw.get("exit_engine")
    if cfg is None:
        print("[runner] exit engine: not configured; a filled position is "
              "UNPROTECTED (add an 'exit_engine' block)")
        return False
    if not isinstance(cfg, dict):
        print("[runner] ERROR: exit_engine must be an object")
        return True
    from blue_engine.core import protective_orders
    from blue_engine.core.exit_engine import ExitConfig, ExitEngine, protecting
    from blue_engine.portfolio.optim_policy import peek_kill_switch, trip_kill_switch

    config = ExitConfig.from_dict(cfg)
    if not config.enabled or filled <= 0:
        print("[runner] exit engine: disabled or nothing filled; skipped")
        return False
    engine = ExitEngine(
        client, config, registry=protective_orders,
        kill_check=lambda: bool(peek_kill_switch().get("tripped")),
        log=print)
    max_cycles = cfg.get("max_cycles")
    hold = cfg.get("supervise_seconds")
    if hold is not None:
        deadline_cycles = max(
            1, int(float(hold) / max(1.0, config.check_interval_seconds)))
        max_cycles = deadline_cycles if max_cycles is None else min(
            int(max_cycles), deadline_cycles)
    stop_event = threading.Event()
    report = engine.run_forever(stop_event=stop_event,
                                max_cycles=max_cycles)
    print(f"[runner] exit engine: checked={report.get('checked')} "
          f"protected={len(report.get('protected') or [])} "
          f"placed={len(report.get('placed') or [])} "
          f"critical={report.get('critical')}")
    for warning in report.get("warnings") or []:
        print(f"[runner] exit engine warning: {warning}")
    for error in report.get("errors") or []:
        print(f"[runner] exit engine ERROR: {error}")
    if report.get("critical") or not protecting(report):
        reason = "; ".join(report.get("errors") or []) or "positions unverified"
        trip_kill_switch(f"exit engine: {reason}")
        print("[runner] CRITICAL: protection could not be established; "
              "kill switch tripped")
        return True
    return False


def main(argv: list[str] | None = None) -> int:
    """CLI entry that loads a JSON config file and runs the engine.

    Returns 0 on success, non-zero on error.
    """
    argv = list(sys.argv[1:] if argv is None else argv)
    paper = "--paper" in argv
    positional = [a for a in argv if not a.startswith("--")]
    config_path = positional[0] if positional else DEFAULT_CONFIG

    print(f"[runner] loading config: {config_path}")
    try:
        raw = load_raw_config(config_path)
    except (FileNotFoundError, ValueError) as exc:
        print(f"[runner] ERROR: {exc}")
        return 2
    return run_with_config(raw, paper=paper)


if __name__ == "__main__":
    sys.exit(main())
