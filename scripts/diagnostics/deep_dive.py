"""Deep-dive ONE real INFY pattern occurrence and print the full state.

The batch runner reports only summary lines, which hides WHY the
confirmation never fires. This prints every LTF step for a single real
pattern so the cause is visible rather than inferred.
"""
from __future__ import annotations

import contextlib
import csv
import io
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "Blue_engine"))

import e2e_replay as e2e  # noqa: E402

TARGET_DATE = sys.argv[1] if len(sys.argv) > 1 else "2026-09-17"


def iso(ts):
    try:
        return datetime.fromtimestamp(float(ts), tz=timezone.utc).strftime("%Y-%m-%d")
    except (TypeError, ValueError):
        return str(ts)[:10]


def load(path):
    out = []
    with path.open(encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            out.append({"datetime": iso(r["datetime"]),
                        "open": float(r["open"]), "high": float(r["high"]),
                        "low": float(r["low"]), "close": float(r["close"]),
                        "volume": float(r["volume"])})
    return out


daily_all = load(HERE / "infy_daily.csv")
ltf_all = load(HERE / "infy_1min.csv")

idx = next(i for i, r in enumerate(daily_all) if r["datetime"] == TARGET_DATE)
daily = daily_all[: idx + 1]
start = next(i for i, r in enumerate(ltf_all) if r["datetime"] >= TARGET_DATE)
ltf = ltf_all[start: start + 200]

print(f"daily frame : {len(daily)} bars ending {daily[-1]['datetime']}")
print(f"ltf frame   : {len(ltf)} bars {ltf[0]['datetime']} .. "
      f"{ltf[-1]['datetime']}")
print(f"ltf close range: {min(r['close'] for r in ltf):.2f} .. "
      f"{max(r['close'] for r in ltf):.2f}")

# What level does the signal derive?
from blue_engine.alpha.Statergy.ocl_keylevel import OCLKeylevelSignal  # noqa
from blue_engine.core.band import BuyerInitiatedDailyVWAP  # noqa

client = e2e.RecordingClient(daily=daily, ltf=ltf)
client.advance_on_quote = True
client.zscore_bars_needed = 5 + 30 + 10

sig = OCLKeylevelSignal(client, {
    "symbol": "INFY", "exchange": "NSE",
    "htf_interval": "day", "ltf_interval": "1minute",
    "band_interval": "day", "band_ceiling_pct": 0.5,
    "pattern_type": "RRG", "min_body_pct": 0.1,
    "trade_direction": "AUTO", "action": "BUY", "quantity": 100,
    "poll_seconds": 0, "one_shot": True, "exec_mode": "ONE_SHOT",
    "ltf_touch_buffer": 0.25,
    "confirm_enabled": True,
    "roc_period": 5, "z_window": 30, "entry_z": 0.5,
    "strategy_label": "REAL",
})

buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    sig._scan_htf_pattern()

lvl = sig._trigger_level
print(f"\npattern detected : {sig._pattern_detected}")
print(f"trigger level    : {lvl}")
hi = max(r["high"] for r in ltf)
lo = min(r["low"] for r in ltf)
print(f"ltf high/low     : {hi:.2f} / {lo:.2f}")
if lvl is not None:
    touched = [i for i, r in enumerate(ltf)
               if r["low"] <= lvl + 25.0]
    print(f"bars whose LOW reaches level+buffer({lvl + 25:.2f}): "
          f"{len(touched)}")
    if touched:
        print(f"  first touch at bar {touched[0]} "
              f"close={ltf[touched[0]]['close']:.2f}")

# Now walk and show the confirm state each step.
print("\n=== LTF walk: confirm state per bar ===")
fired = None
for i in range(len(ltf)):
    client.ltf_cursor = i
    client.daily_cursor = len(daily)
    with contextlib.redirect_stdout(io.StringIO()):
        res = sig._monitor_ltf_touch()
    if i % 20 == 0 or res:
        state = sig._confirm.state if sig._confirm else "n/a"
        zs = sig._roc_z_series(client.history(interval="1minute")
                               .get("data", []))
        z_now = None
        if zs:
            z_now = zs[-1]
        print(f"  bar {i:>3} ltp={ltf[i]['close']:>8.2f} state={state:<9} "
              f"z={z_now if z_now is None else round(z_now, 3)}")
    if res:
        fired = res
        print(f"  bar {i:>3} *** FIRED ***")
        break

if fired is None:
    print("\nNo intent. Final state:", sig._confirm.state)
    if sig._confirm is not None:
        print("  arm_idx:", sig._confirm.s.arm_idx)
        print("  level  :", sig._confirm.s.level)
    zs = sig._roc_z_series(client.history(interval="1minute").get("data", []))
    nz = [z for z in zs if z is not None]
    print(f"  z values computed: {len(nz)} of {len(zs)}")
    if nz:
        print(f"  z range: {min(nz):.3f} .. {max(nz):.3f}")
        print(f"  bars with z > 0.5: {sum(1 for z in nz if z > 0.5)}")