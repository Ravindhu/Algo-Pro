# pov_engine/input/

User-editable JSON configs. **No Python knowledge required** to add a new run.

## Master JSON format (recommended)

The `master.json` file uses a **toggle-style** format. Set each strategy
to `true` or `false` to enable/disable it. Each enabled strategy must
have its own config block at the top level with the same name.

```json
{
  "strategy_label": "POV-INFY-NSE",
  "symbol":   "INFY",
  "exchange": "NSE",

  "strategies": {
    "keylevel": false,
    "pov":      true
  },

  "keylevel": {
    "trigger_price": 1480.0,
    "trigger_direction": "ABOVE",
    "poll_seconds": 5,
    "wait_seconds": 1800,
    "action": "BUY",
    "quantity": 1000
  },

  "pov": {
    "action":                  "BUY",
    "quantity":                1000,
    "participation_pct":       10.0,
    "duration_seconds":        900,
    "poll_interval_seconds":   15,
    "product":                 "CNC",
    "order_mode":              "LIMIT",
    "max_slice_quantity":      100,
    "ceiling_price":           1500,
    "stop_loss_price":         1450,
    "exit_participation_pct":  30.0,
    "limit_expiry_pct":        80.0,
    "cutoff_time":             "15:15",
    "squareoff_time":          "15:20"
  }
}
```

### Combinations you can pick

| `strategies` block | Result |
|---|---|
| `{ "pov": true }` | **Manual mode** — POV runs immediately with `pov.action` and `pov.quantity`. (Replaces the old `manual` signal.) |
| `{ "keylevel": true, "pov": true }` | Wait for the LTP to cross `keylevel.trigger_price`, then hand off to POV. |
| `{ "pov": false }` (no slicer) | Error: "no slicer enabled" |
| `{ "keylevel": true, "pov": false }` | Error: "no slicer" (a signal needs a slicer) |
| `{ "keylevel": true, "pov": true }` and keylevel config block missing | Error: "strategy 'keylevel' is enabled but has no config block" |

### Field reference — top level

| Key | Type | Required | Description |
|---|---|---|---|
| `strategy_label` | string | no | Tag visible in the OpenAlgo orderbook. Defaults to `"POV"`. |
| `api_key` | string/null | no | OpenAlgo API key. `null` = read from `OPENALGO_API_KEY` env var. |
| `host` | string/null | no | OpenAlgo REST URL. `null` = read from `HOST_SERVER` env var. |
| `ws_url` | string/null | no | WebSocket URL. `null` = read from `WEBSOCKET_URL` env var. |
| `symbol` | string | yes | Trading symbol, e.g. `INFY`. Shared by all enabled strategies. |
| `exchange` | string | yes | Exchange code, e.g. `NSE`, `NFO`, `BSE`, `MCX`. |
| `strategies` | object | yes | `{strategy_name: true/false}`. Each enabled strategy must have a matching config block. |
| `keylevel` | object | when enabled | Keylevel signal config (see below). |
| `pov` | object | when enabled | POV slicer config (see below). |

### Depth-aware execution guards (OpenAlgo-SDK-style)

Three optional guards inside the `pov` block wrap the OpenAlgo SDK's
`client.depth(...)` and `client.quotes(...)` calls to add L2-aware safety:

1. **Market-Depth LIMIT Pricing** — instead of pricing at a static
   `ceiling_price`, the LIMIT child sits on the best bid (BUY) or best
   ask (SELL) and is clamped to never cross the spread.
2. **Depth Slice Cap** — the calculated POV slice is capped to the
   displayed quantity on the best opposite-side tier. If the top tier is
   thinner than `depth_slice_min_qty`, the child is skipped (avoids dust
   orders on a thin book).
3. **Circuit Guard** — if the LTP moves within `circuit_buffer_pct` of
   the upper or lower circuit limit, the execution loop pauses
   automatically (avoids persistent broker rejections and capital
   lock-up near the circuit).

All three guards default to **enabled**. When `client.depth(...)` fails
or returns no top-of-book, every guard degrades to the pre-existing
behaviour (static `ceiling_price` / `max_slice_quantity`); the run NEVER
stalls because depth was unavailable.

| Key | Type | Default | Description |
|---|---|---|---|
| `depth_aware_pricing` | bool | `true` | Enable market-depth LIMIT pricing. |
| `depth_offset_ticks` | int | `1` | **Number of ticks inside the spread.** Default `1` means LIMIT orders are placed at `best_bid + 1 tick` for BUY (or `best_ask - 1 tick` for SELL) — the engine "joins the inside of the spread". Set to `0` to sit exactly on the bid/ask (legacy behaviour). Set to `2` (or higher) to sit two or more ticks inside — useful for very illiquid names with wide spreads. |
| `tick_size` | float/null | `null` | Override tick size; otherwise inferred from the quote payload (fallback `0.05`). |
| `depth_slice_cap` | bool | `true` | Enable top-of-book slice cap. |
| `depth_slice_min_qty` | int | `1` | If best opposite tier has fewer than this many shares, skip the child instead of placing a dust order. |
| `circuit_guard_enabled` | bool | `true` | Enable the circuit-limit safety valve. |
| `circuit_buffer_pct` | float | `0.5` | Pause threshold percent (0.5 = within 0.5% of either circuit). |

### Keylevel signal config (inside the `"keylevel"` block)

| Key | Type | Required | Description |
|---|---|---|---|
| `trigger_price` | float | yes | The price level that fires the signal. |
| `trigger_direction` | string | yes | `ABOVE` (LTP >= trigger) or `BELOW` (LTP <= trigger). |
| `poll_seconds` | int | no | LTP polling interval while waiting. Default 5. |
| `tolerance` | float | no | Cross tolerance in price units. Default 0.0. |
| `one_shot` | bool | no | Fire only once. Default `true`. |
| `wait_seconds` | int/null | no | Optional cap on how long to wait. `null` = wait forever. |
| `action` | string | no | `BUY` or `SELL`. Default `BUY`. |
| `quantity` | int | no | Target quantity. Default 0. |

### POV slicer config (inside the `"pov"` block)

| Key | Type | Required | Description |
|---|---|---|---|
| `action` | string | yes (manual mode) | `BUY` or `SELL`. Used only when no signal is enabled. |
| `quantity` | int | yes (manual mode) | Total target quantity. Used only when no signal is enabled. |
| `participation_pct` | float | yes | Each child = `delta_volume * participation_pct / 100`. Range (0, 100]. |
| `duration_seconds` | int | yes | Hard upper bound on the POV loop. |
| `poll_interval_seconds` | int | yes | Seconds between quote polls. |
| `product` | string | yes | `CNC`, `MIS`, or `NRML`. |
| `order_mode` | string | yes | `MARKET` or `LIMIT`. |
| `max_slice_quantity` | int/null | no | Cap on each child order. `null` = no cap. |
| `ceiling_price` | float/null | no | Required for `LIMIT` BUY. Children are skipped if LTP > ceiling. |
| `stop_loss_price` | float/null | no | When LTP drops here, slicer switches to `exit_participation_pct`. BUY only. |
| `exit_participation_pct` | float | no | Participation rate used during stop-loss, cutoff, and squareoff phases. |
| `limit_expiry_pct` | float | no | Threshold (0-100) for cancelling stale LIMIT children. Default 80. |
| `cutoff_time` | `HH:MM`/null | no | IST time after which slicer uses `exit_participation_pct`. |
| `squareoff_time` | `HH:MM`/null | no | IST time after which slicer aggressively exits. |
| `depth_aware_pricing` | bool | no | See "Depth-aware execution guards" above. Default `true`. |
| `depth_offset_ticks` | int | no | Ticks inside the spread. Default `1` (join the inside of the spread). Set `0` for the legacy "sit on the bid/ask" behaviour. |
| `tick_size` | float/null | no | Tick size override. Default: read from quote. |
| `depth_slice_cap` | bool | no | Cap slice to best opposite tier. Default `true`. |
| `depth_slice_min_qty` | int | no | Skip child if best tier below this. Default `1`. |
| `circuit_guard_enabled` | bool | no | Enable circuit safety valve. Default `true`. |
| `circuit_buffer_pct` | float | no | Pause threshold %. Default `0.5`. |

### Exchange & Compliance (Feature 5-8)

The following knobs enable SME / PCAS / restricted-segment safety. Leave
them at their defaults for plain NSE equity runs.

| Key | Type | Default | Description |
|---|---|---|---|
| `sme_periodic_call_auction` | bool | `false` | Feature 5 - flag the run as SME / PCAS. When `true` the auction-window guard only allows slice placement inside the configured windows. Auto-detected from the symbol master if omitted. |
| `auction_windows` | list[str] | `["09:15-15:30"]` | Feature 5 - list of `HH:MM-HH:MM` windows the guard will allow on SME/PCAS runs. |
| `auction_window_enabled` | bool | `true` | Master switch for the auction-window guard. |
| `auction_price_enabled` | bool | `true` | Feature 6 - master switch for the auction-price guard (anchor to `auction_price` instead of LTP). |
| `lot_size` | int/null | `null` | Feature 7 - lot size resolution. See "Lot-size resolution" below for the full precedence. |
| `restricted_same_day_reverse_exit` | bool | `false` | Feature 8 - explicit override; auto-detected from T2T/BE/ST series when omitted. |
| `supervisor_override` | bool | `false` | Feature 8 - set `true` to release the same-day reverse-exit block (logged as a supervisor action). |

#### Lot-size resolution (Feature 7 detail)

The `lot_size` field has **three resolution paths**, in priority order:

1. **`lot_size` set in the JSON** (e.g. `"lot_size": 1200`) - **always wins**.
   Use this when you know the lot size and want deterministic
   behaviour, or when the broker master is unavailable.
2. **Automatic from broker master contract** - the POV engine reads
   `symtoken.lotsize` from the platform's master contract database.
   The master is downloaded automatically the first time you click
   "Master Contract" in the OpenAlgo Settings page (or run the
   `master_contract_download` API endpoint). m.Stock, Zerodha, Angel,
   dhan, etc. all populate `lotsize` for every symbol including
   SME / PCAS tokens. **This is the default and recommended path.**
3. **SDK fallback** - if the platform master DB is unavailable AND
   the OpenAlgo SDK exposes a `client.symbol(symbol, exchange)`
   method, that method is called. The current OpenAlgo SDK (v2.0.3)
   does NOT expose this, so for m.Stock today path #3 is a no-op.
   The fallback exists for future SDK releases and for custom broker
   adapters built outside the OpenAlgo platform.

If none of the three paths returns a value, the slicer logs
`"no lot size resolved; rounding disabled"` and proceeds with the
un-rounded slice quantity. No silent zero-share orders, no exception.

The same lookup path works for **plain equity AND for SME / PCAS
scrips** - there is no special-case short-circuit. So once the
m.Stock master is downloaded, lot rounding for an SME token like
`XYZ-SME` with `lotsize=1200` is automatic, with no JSON change.

### System Stability & State Preservation (Feature 9-12)

These knobs power the durable child-order ledger, the throttle gate,
and the WebSocket fill stream. They are read once at startup and
honoured on every poll.

| Key | Type | Default | Description |
|---|---|---|---|
| `max_order_to_trade_ratio` | float | `10.0` | Feature 10 - hard cap on (orders / trades) to avoid OTR penalties. |
| `min_order_interval_seconds` | float | `1.0` | Feature 10 - minimum gap between any two child orders. |
| `cancellation_cool_off_seconds` | float | `2.0` | Feature 10 - mandatory pause after a cancel. |
| `quote_failure_threshold` | int | `3` | Feature 12 - consecutive invalid quote frames before the loop transitions to PAUSED. |

The Feature 9 persistent ledger uses the platform's `database.pov_db`
when importable; otherwise the framework runs in-memory only with a
warning on startup. Feature 11 subscribes to the order-update event
stream (platform event-bus preferred, SDK WebSocket as fallback).

## Dynamic Rate Control (Feature 13 - VWAP Band + Slippage Feedback)

Two new phases run on every poll, on top of the existing
`_select_participation` logic. The new behaviour is **opt-out**:
set `vwap_filter_enabled = false` and/or `slippage_guard_enabled = false`
in the `pov` block to restore the legacy rate logic.

### Phase 1 - VWAP Value Filter (dynamic ceiling + floor)

The band is anchored on the **buyer-initiated VWAP**: on every poll the
slicer aggregates `ltp * delta_volume` only for ticks that were
buyer-initiated (`LTP >= ask`). This tracks where buyers are actually
transacting rather than the all-trade session VWAP. Until the first
buyer-initiated tick arrives the slicer falls back to the all-trade session
VWAP. The slicer derives a **dynamic band** `[floor, ceiling]` around it:

```
ceiling = vwap * (1 + vwap_ceiling_pct / 100)   # default +0.5%
floor   = vwap * (1 - vwap_floor_pct   / 100)   # default -3.0%
```

and picks a POV rate per poll using this decision tree:

| Condition (BUY)                       | Result                     |
|---|---|
| `LTP >= ceiling`                      | **HALT** - slice suppressed (return 0) |
| `LTP <= floor`                        | passthrough (use `part_pct`) |
| `floor < LTP < ceiling`               | **interpolate** between `min_pov_pct` and `max_pov_pct` |

For SELL the sides flip: HALT is at `floor` (don't undersell),
passthrough is at `ceiling` (sell into strength).

The interpolated rate is driven by the position of LTP inside the band:

```
f_band = clamp01((ceiling - LTP) / (ceiling - floor))     # 0 at ceil, 1 at floor

rate = lerp(min_pov, max_pov, f_band)
```

### Phase 2 - Slippage Feedback Loop

When a child order fills, `pov_engine.core.order_updates` records
one slippage sample in basis points:

```
expected = max(session_vwap, arrival_vwap)
bps      = (fill_price - expected) / expected * 10_000   (BUY)
```

The `arrival_vwap` is the volume-weighted mid-quote VWAP of the
`arrival_window_ticks` quotes observed between the `placeorder`
send and the fill event. This is the **institutional arrival-price**
benchmark and strips out our own impact.

The slicer keeps a rolling mean of the last `slippage_window_fills`
samples. When the mean crosses `max_slippage_bps`, the rate is
multiplied by `slippage_penalty_factor` and clamped to
`min_pov_pct`:

```
rolling_bps = mean(slippage_samples)
if rolling_bps >= max_slippage_bps:
    final_pct = max(raw_pct * slippage_penalty_factor, min_pov_pct)
```

### Knobs

| Key | Type | Default | Description |
|---|---|---|---|
| `vwap_filter_enabled` | bool | `true` | Set to `false` to disable Phase 1 entirely. |
| `vwap_ceiling_pct` | float | `0.5` | Percent ABOVE buyer-initiated VWAP that defines the HALT band. |
| `vwap_floor_pct` | float | `3.0` | Percent BELOW buyer-initiated VWAP that defines the floor. |
| `min_pov_pct` | float | `5.0` | Lower clamp for the interpolated rate AND the slippage penalty floor. |
| `max_pov_pct` | float | `25.0` | Upper clamp for the interpolated rate. |
| `passthrough_floor` | string | `"part_pct"` | What to use when LTP < floor (BUY). One of `part_pct` / `max_pov` / `min_pov`. |
| `slippage_guard_enabled` | bool | `true` | Set to `false` to disable Phase 2 entirely. |
| `max_slippage_bps` | float | `50.0` | Rolling-mean slippage threshold that triggers the penalty. |
| `slippage_penalty_factor` | float | `0.5` | Multiplier applied to the rate when the threshold is crossed. |
| `slippage_window_fills` | int | `10` | Number of recent fills used for the rolling-mean slippage. |
| `arrival_window_ticks` | int | `20` | Number of mid-quote ticks kept per open child for the arrival-price VWAP. |

### Tuning recipes (copy/paste any of these into the `pov` block)

1. **Conservative (default-ish)**: tight band, moderate slippage clamp.
   ```jsonc
   "vwap_filter_enabled":     true,
   "vwap_ceiling_pct":        0.5,
   "vwap_floor_pct":          3.0,
   "min_pov_pct":             5.0,
   "max_pov_pct":             25.0,
   "slippage_guard_enabled":  true,
   "max_slippage_bps":        50.0,
   "slippage_penalty_factor": 0.5
   ```

2. **Aggressive**: wider rate range, harsh slippage penalty.
   ```jsonc
   "min_pov_pct":             10.0,
   "max_pov_pct":             40.0,
   "max_slippage_bps":        30.0,
   "slippage_penalty_factor": 0.25
   ```
3. **Legacy-compatible**: turn BOTH features off.
   ```jsonc
   "vwap_filter_enabled":     false,
   "slippage_guard_enabled":  false
   ```

## Legacy configs (still supported)

`pov_manual.json` and `keylevel_pov.json` use the **old** single-pair
format (`signal` + `slicer` keys). They are kept for backward
compatibility and as examples. New configs should use `master.json`.

## How mode is handled

The OpenAlgo mode (`live` vs `analyze`) is **never** controlled by this
config. It is set in the OpenAlgo UI (Profile -> Mode toggle, or
`POST /api/v1/analyzer/toggle`). The runner only reads the current mode
via `client.analyzerstatus()` and prints it for your awareness.
