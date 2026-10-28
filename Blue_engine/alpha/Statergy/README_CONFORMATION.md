# Conformation entry model (`entry_model = "conformation"`)

A **later, stricter** entry for the OCL strategy. The shipped model fires on the
first LTF touch of the HTF key level. This one refuses to fire until the whole
smart-money sequence is visible on closed bars.

It lives **inside `ocl_keylevel.py`** — not in a separate module. The OCL code
is reused on all three frames, and SL / TP / R-multiple / sizing / trailing
stops are the ones that already exist. Nothing is duplicated.

---

## The three frames

| Frame | Example | OCL role | Code |
|---|---|---|---|
| **MHTF** | monthly | its key level is the level to be mitigated; its direction is the bias | `_conformation_scan()` on `mhtf_interval` |
| **HTF** | daily | mitigation → stop hunt → IFVG → CISD → CISD mitigation → **daily OCL pattern** | `_conformation_step()` |
| **LTF** | 15 min | mitigates the daily key level → **OCL BUY trigger** | `_conformation_entry()` |

Configure the intervals with `mhtf_interval`, `htf_interval`, `ltf_interval`.

## The chain, in order

```
0  MHTF pattern formed at a MONTHLY CLOSE -> level ladder + bias
1  HTF wick enters the current ladder rung        (mitigation)
2  HTF sweeps a confirmed fractal swing low and closes back above it
3  HTF bullish displacement closes through a bearish FVG            (IFVG)
4  CISD = the OPEN of c1, the oldest candle of the consecutive red run
   that holds that FVG; confirmed on a close above it
5  price returns to the CISD level                   (CISD mitigation)
6  a daily OCL pattern forms (RRG: red, red, green closing above C2 open)
7  15m mitigates the daily key level, then the OCL BUY trigger fires
```

### CISD — the exact definition

> "first red candle open of consecutive down candles with IFVG, and then price
> moves and closes above the red candle open — this level is CISD."

Operationally, on the HTF (daily):

- take the consecutive **red** candles immediately before the displacement,
- the bearish FVG must sit **inside** that run (its 3-candle window is the
  floor for the walk-back),
- the level is the **OPEN of the oldest candle of the run** (`c1`), never the
  deepest and never the last.

### The level ladder

| MHTF pattern | Ladder (top → bottom) |
|---|---|
| `OCL` | aggressive (C2 open / C3 close) → conservative (C3 open) |
| `SELL_FAILURE_TOP` | aggressive (broken C3 open) → conservative (C2 open) |
| `SELL_FAILURE_BOTTOM` | single level (lowest swing open) |

Conformation always runs at the **current MHTF rung**. A daily **close below the
swing low** kills that rung and the chain waits for the next rung below,
restarting the whole sequence there.

### The daily ladder is walked too

The daily OCL pattern has its **own** ladder, treated identically:

1. the daily pattern arms `htf_ladder[0]`;
2. a **daily close below that rung's level** steps down to `htf_ladder[1]`;
3. when the daily ladder is exhausted the chain returns to the CISD gate and
   waits for a **new CISD** — it never reuses the old one.

### The monthly stop is terminal

Writing `M = min(mhtf_ladder)` (the bottom MHTF rung):

- **a MONTHLY close below `M`** ends the chain (`MHTF_DEAD`). No new CISD is
  hunted until a fresh MHTF pattern forms.
- A **daily** close below `M`, a wick through it, or any number of failed daily
  attempts do **not** end the chain — only a monthly close does.

State transitions now logged: `RUNG_DEAD` (MHTF rung), `HTF_RUNG_DEAD` (daily
rung), `MHTF_DEAD` (terminal), `DAILY_RUNG_DEAD` (swing low broken after the
daily gate → new CISD required).

A monthly `SELL_FAILURE_TOP` is **not** a blocker: it flips the bias to bullish
once the sell fails, exactly as the LTF blueprint does, and the retracement is
then hunted through the same ladder.

### Exits

`SL = swing low − sl_buffer` (absolute price buffer), with the hard rules
`SL < swing_low` and `SL < entry`. The class's own machinery then owns the
trade: `target_rr`, breakeven, ATR / DV-ATR trailing stop, HTF/LTF stops.

Costs are **not** charged in the offline evaluation. Execution happens once, on
the 15m frame, so one round trip is the correct charge and the live stack
applies it.

---

## Enabling it

`entry_model` defaults to `"aggressive"`, so **nothing changes until you ask**:

```json
{
  "symbol": "INFY",
  "exchange": "NSE",
  "htf_interval": "day",
  "ltf_interval": "15minute",
  "mhtf_interval": "1month",
  "entry_model": "conformation",
  "setup_mode": "OCL"
}
```

- `setup_mode` selects which blueprint the chain runs (`OCL`,
  `SELL_FAILURE_TOP`, `SELL_FAILURE_BOTTOM`).
- Both keys are in `params_space`, so the optimizer can search
  `entry_model × setup_mode` like any other parameter.

## Offline evaluation

```python
sig = OCLKeylevelSignal(None, {"symbol": "INFY", "exchange": "NSE",
                               "entry_model": "conformation"})
out = sig.evaluate_bars(ltf_bars, htf_bars, mhtf_bars)
out["signals"]        # one entry per completed chain
out["transitions"]    # every state change, with timestamp and reason
out["rejections"]     # where the chain stopped and why
```

`evaluate_bars` walks the LTF bars once and feeds each one only the HTF/MHTF
history that had **closed** by that timestamp. No look-ahead.

## Config keys

| Key | Default | Meaning |
|---|---|---|
| `entry_model` | `"aggressive"` | `aggressive` or `conformation` |
| `mhtf_interval` | `"1month"` | MHTF frame |
| `swing_lookback` | 3 | bars each side of a fractal |
| `displacement_mult` | 1.5 | displacement body vs 20-bar average |
| `displacement_avg_bars` | 20 | bars in that average |
| `displacement_close_frac` | 0.33 | close must sit in the top third |
| `fvg_min_size` | 0.0 | minimum gap height, price units |
| `ifvg_confirm` | `"body_close"` | or `"wick"` |
| `cisd_close_required` | True | False = a wick above is enough |
| `cisd_max_lookback` | 10 | cap on the red-run walk-back |
| `sl_buffer` | 0.0 | absolute buffer below the swing low |
| `target_rr`, `atr_*`, `breakeven_rr` | inherited | see the main class |

## Assumptions (stated, not hidden)

1. **MHTF bias** = a BUY pattern on the MHTF. A monthly `SELL_FAILURE_TOP`
   flips to bullish after the break (see above). A bearish MHTF that has not
   failed blocks the chain.
2. **CISD** = the open of `c1`, the oldest red candle of the run that contains
   the FVG.
3. **Rung death** = a **close** below the swing low. Wicks do not kill a rung.
4. **Daily OCL pattern** is required before the 15m is consulted, and its
   nearest level becomes the 15m key level (CISD is the fallback).
5. **One live chain at a time**; one MHTF rung produces one sequence.
6. **Exits are the class's existing ones** — this model adds entry
   conformation, not a new exit model.
7. **No costs** in `evaluate_bars`; a single round trip is charged at
   execution.

## Tests

```powershell
.\.venv\Scripts\python.exe -m pytest Blue_engine\tests\test_ocl_conformation.py -q
```

The end-to-end fixture asserts its own preconditions (fractal, FVG, gap
position, displacement, CISD candle) before the test runs — so a broken fixture
fails as a fixture error, not disguised as a detector failure.
