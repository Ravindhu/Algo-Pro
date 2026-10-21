"""Run the Economic engine on current macro data and persist a fresh run.

Why this is needed
------------------
The Economic Dash page renders the LAST PERSISTED RUN from
``data/economic_runs/YYYY-MM-DD.jsonl`` - not live config. So correcting
``macro_master.json`` changes nothing on screen until a new run is recorded.
That is why the page kept showing USD/local FX 84.50 after the value was fixed
to the FBIL rate.

This script performs one full, read-only economic run and appends it to the run
history. It uses the same provider stack the production runner uses, so the run
it writes is representative.

Safety: read-only. It calls no order endpoint and touches no broker state. The
only write is the run-history JSONL (append-only, crash-safe).

Usage (from my-trading-system/):
    openalgo\\Scripts\\python.exe Scripts\\run_economic_engine.py <apikey>
    # without an api key, it runs on static config macro only:
    openalgo\\Scripts\\python.exe Scripts\\run_economic_engine.py
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "Blue_engine"))

KEY = sys.argv[1] if len(sys.argv) > 1 else ""


def main() -> int:
    from blue_engine.economics.economic_engine import EconomicEngine
    from blue_engine.economics import run_history

    cfg = json.loads(
        (ROOT / "Blue_engine" / "input" / "macro_master.json")
        .read_text(encoding="utf-8"))
    econ_cfg = cfg["engines"]["economic"]

    # --- assemble macro exactly like the runner does -----------------------
    macro = dict(econ_cfg.get("macro") or {})
    provider_note = "static config only"

    try:
        from blue_engine.economics.providers import build_default_stack

        client = None
        if KEY:
            from blue_engine.core.client import init_client
            client = init_client(api_key=KEY)

        logs = []
        stack = build_default_stack(client=client, config={}, log=logs.append)
        if stack is not None:
            live = stack.fetch()
            live_meta = live.pop("meta", {}) or {}
            # Live values override the static block, per the composite contract.
            for k, v in live.items():
                if isinstance(v, dict) and v.get("value") is not None:
                    macro[k] = v
            prov = live_meta.get("provenance") or {}
            provider_note = (f"composite; live for "
                             f"{sorted(prov.keys()) or 'nothing'}")
            for w in (live_meta.get("warnings") or [])[:5]:
                print(f"  provider warn: {str(w)[:95]}")
    except Exception as exc:  # noqa: BLE001
        print(f"  provider stack unavailable ({exc!r}); using static macro")

    print(f"  macro source: {provider_note}")
    cur = (macro.get("currency") or {})
    print(f"  currency -> {cur.get('value')}  src={cur.get('source')}")

    # --- run the engine ----------------------------------------------------
    eng = EconomicEngine({
        "signals": econ_cfg.get("signals") or {},
        "blend": econ_cfg.get("blend"),
        "use_surprise": econ_cfg.get("use_surprise", True),
        "use_delevel": econ_cfg.get("use_delevel", True),
        "use_nlp": econ_cfg.get("use_nlp", False),
        "regime_model": econ_cfg.get("regime_model", "none"),
        "model_weight": econ_cfg.get("model_weight", 0.0),
    })
    res = eng.run(context={"macro": macro})
    data = getattr(res, "data", None) or {}
    if not data:
        print(f"  engine FAILED: {getattr(res, 'error', None)}")
        return 1

    print()
    print("  regime   :", data.get("regime"))
    print("  risk-on  :", data.get("risk_on_score"))
    print("  gross    :", data.get("gross_exposure_multiplier"))
    sigs = data.get("signals") or {}
    print(f"  signals  : {len(sigs)}")
    for k, s in sigs.items():
        src = s.get("source") or "config fallback"
        print(f"     {k:<16} {str(s.get('value')):>10}  {src[:52]}")

    # --- persist -----------------------------------------------------------
    payload = {
        "regime": data.get("regime"),
        "risk_on_score": data.get("risk_on_score"),
        "gross_exposure_multiplier": data.get("gross_exposure_multiplier"),
        "multiplier_detail": data.get("multiplier_detail"),
        "regime_model": data.get("regime_model"),
        "signals": sigs,
        "signals_available": len(sigs),
        "warnings": data.get("warnings") or [],
        "provider_note": provider_note,
    }
    path = run_history.record(payload)
    print()
    print(f"  persisted -> {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())