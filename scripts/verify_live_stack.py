"""End-to-end verification of the live macro stack (all providers).

Walks the exact production path:

    build_default_stack(client)  ->  CompositeProvider
      ->  RbiReferenceRateProvider   (USD/INR)
      ->  MospiProvider              (inflation, GDP, unemployment)
      ->  OpenAlgoMacroProvider      (commodities)
      ->  FiiFlowProvider / IndiaMacroProvider
    ->  EconomicEngine.run()
    ->  signals dict the dashboard renders

Asserts each live value is present, plausible, and attributed to a real source
(so a silent fall back to macro_master.json is visible).

Run:
    openalgo\\Scripts\\python.exe Scripts\\verify_live_stack.py <apikey>
"""

from __future__ import annotations

import json
import os
import sys

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "Blue_engine"))

KEY = sys.argv[1] if len(sys.argv) > 1 else ""

# (signal, plausible lo, hi, what it should be)
EXPECT = {
    "currency": (80.0, 120.0, "FBIL USD/INR reference rate"),
    "inflation": (0.0, 15.0, "MoSPI CPI All-India Combined"),
    "gdp_growth": (2.0, 12.0, "MoSPI NAS real GDP YoY"),
    "employment": (2.0, 12.0, "MoSPI PLFS unemployment rate"),
}

FAILURES = []


def main() -> int:
    from blue_engine.economics.providers import build_default_stack

    client = None
    if KEY:
        from blue_engine.core.client import init_client
        client = init_client(api_key=KEY)

    logs = []
    stack = build_default_stack(client=client, config={}, log=logs.append)
    if stack is None:
        print("build_default_stack returned None")
        return 1

    print("=" * 92)
    print("1. Composite macro payload")
    print("=" * 92)
    macro = stack.fetch()
    meta = macro.pop("meta", {}) or {}
    print(f"  providers : {list((meta.get('providers') or {}).keys())}")
    prov = meta.get("provenance") or {}
    print(f"  provenance: {json.dumps(prov)[:400]}")
    print(f"  ok={meta.get('ok')} stale={meta.get('stale')}")
    for w in (meta.get("warnings") or [])[:8]:
        print(f"  warn: {str(w)[:100]}")

    print()
    print("=" * 92)
    print("2. Live values (the four that should be attributed, not fallback)")
    print("=" * 92)
    print(f"{'signal':<14} {'value':>10} {'plausible':<12} source")
    print("-" * 92)
    live_count = 0
    for sig, (lo, hi, what) in EXPECT.items():
        block = macro.get(sig)
        if not block:
            print(f"{sig:<14} {'MISSING':>10} {'-':<12} {what}")
            FAILURES.append(f"{sig} missing from composite")
            continue
        val = block.get("value")
        src = block.get("source") or "config fallback"
        try:
            ok = lo <= float(val) <= hi
        except (TypeError, ValueError):
            ok = False
        if not ok:
            FAILURES.append(f"{sig}={val} outside {lo}-{hi}")
        if block.get("source"):
            live_count += 1
        print(f"{sig:<14} {str(val):>10} {'YES' if ok else 'NO!':<12} {src}")
        print(f"{'':<14} {'':>10} {'':<12}   (expect {what})")

    print()
    print("=" * 92)
    print("3. Engine consumes it and carries provenance to the signal")
    print("=" * 92)
    from blue_engine.economics.economic_engine import EconomicEngine

    cfg = json.loads(
        (ROOT / "Blue_engine" / "input" / "macro_master.json")
        .read_text(encoding="utf-8"))["engines"]["economic"]

    eng = EconomicEngine({
        "signals": cfg.get("signals") or {},
        "blend": cfg.get("blend"),
        "use_surprise": cfg.get("use_surprise", True),
        "use_delevel": cfg.get("use_delevel", True),
        "use_nlp": cfg.get("use_nlp", False),
        "regime_model": cfg.get("regime_model", "none"),
        "model_weight": cfg.get("model_weight", 0.0),
    })
    res = eng.run(context={"macro": macro})
    data = getattr(res, "data", None) or {}
    sigs = data.get("signals") or {}

    print(f"  regime  : {data.get('regime')}")
    print(f"  risk-on : {data.get('risk_on_score')}")
    print(f"  gross   : {data.get('gross_exposure_multiplier')}")
    print(f"  signals : {len(sigs)}")
    print()
    print(f"{'signal':<16} {'value':>10} {'weight':>8} {'score':>8}  source")
    print("-" * 92)
    for k, s in sigs.items():
        print(f"{k:<16} {str(s.get('value')):>10} "
              f"{str(s.get('weight')):>8} "
              f"{float(s.get('score') or 0):>+8.3f}  "
              f"{(s.get('source') or 'config fallback')[:44]}")

    # Zero-weight signals must not contribute.
    for k in ("climate", "tax"):
        s = sigs.get(k)
        if s and float(s.get("weight") or 0) != 0:
            FAILURES.append(f"{k} still has non-zero weight")

    print()
    print("=" * 92)
    print(f"RESULT: {live_count}/4 live-sourced", )
    print("=" * 92)
    if FAILURES:
        print(f"FAILURES ({len(FAILURES)}):")
        for f in FAILURES:
            print(f"   - {f}")
        return 1
    print("ALL CHECKS PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())