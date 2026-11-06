# Decision 0021: how the POV engine sends orders (stage 6.5)

Status: ACCEPTED 2026-11-06 (owner chose option A). Implementation in progress, step by step.

## Implementation status (2026-11-06)

- DONE: `OpenAlgoClient.place_child` (MARKET or LIMIT) and `OpenAlgoClient.cancel`, both in
  `src/quant/execution/openalgo_client.py`. Shared session guard split out as `_check_session_guard`.
  Tests: `tests/unit/test_live_guard.py` (48 cases in the file incl. older ones).
- Cancel guard choice: a cancel has no `GateDecision`, because it reduces risk. It still needs live mode,
  a recorded approval and an unchanged limits file. Owner to confirm this in review.
- PEG orders: not supported. Need their own note.
- DONE: read-only adapter `src/quant/execution/readonly_client.py` (quotes, depth, orderbook; no order
  methods; tests in `tests/unit/test_readonly_client.py`).
- DONE: executor skeleton `src/quant/execution/pov_executor.py`. Order path per child: duplicate-id check ->
  gate -> `ApprovedOrderQueue` -> `place_child` -> audit. Tests in `tests/unit/test_pov_executor.py`
  (uses the real gate for the kill switch, safe mode and quantity cases).
- Note: the gate's `RiskState` (seen ids, open exposure, P&L) is owned by the caller. The executor's own
  duplicate-id check is a second, local guard.
- TODO: port the POV loop (timing, stale cancels, reconciliation through the read adapter) and the pure helpers.

## The problem

Blue's POV slicer (`Blue_engine/pov/slicers/pov.py`) sends orders itself:

- Line ~691: `self.client.placeorder(...)` sends each child order directly to the broker.
- Line ~873: `client.cancelorder(...)` cancels stale child orders directly.
- It reconciles failed placements by reading `client.orderbook()` itself.
- Its own `pre_order_check` hook is a local check. It is not the quant risk gate.

If this code were copied into `src/quant/` unchanged, it would place orders without:
- the risk gate (`src/quant/risk/gate.py`),
- the approved order queue,
- the live guard in `OpenAlgoClient` (decision 0017).

That breaks two rules in AGENTS.md:
- "Only the Risk gate may pass an order intent to OpenAlgo."
- "Only `execution/openalgo_client.py` may contain `.placeorder(`."

So the POV engine cannot be moved as a straight copy.

## Proposed design

1. **Child intents, not child orders.** The POV slicer produces one `TradeIntent` per child order
   (kind `entry` for building a position, `exit` for reducing one). It does not call the broker.
2. **Gate first.** Each intent goes through `check_intent` in `risk/gate.py`. Only an approved
   `GateDecision` moves on.
3. **Queue, then router.** Approved intents go to `ApprovedOrderQueue`. The router (`execution/router.py`)
   sends them to the client.
4. **One order client, with the live guard.** `OpenAlgoClient` gets three methods, all behind the same
   live guard as `place` (decision 0017):
   - `place_child(intent, api_key, decision, price_type, limit_price)` for MARKET, LIMIT and PEG orders.
     Today it only supports MARKET.
   - `cancel(order_id, strategy, ...)` for cancelling a child order. A cancel is risk-reducing, but it
     still goes through the same guard so there is one code path.
   - No other module may name the order route.
5. **Read-only adapter.** `quotes`, `depth`, `orderbook`, and reconciliation reads go through a separate
   read-only class. It has no order methods, so reads cannot place or cancel.
6. **Injected ports.** The POV loop (timing, child sizing, stale-cancel logic, reconciliation) takes the
   order port and the read port as arguments. Tests use fakes. No test touches the network.
7. **Timing and sizing stay pure.** Child sizing uses `execution/router.py::pov_child_qty` and the
   band logic. The loop does not hold any broker state of its own.

## What this changes in the flow

- The POV slicer now sits between "Signal and sizing" and "Risk gate". It no longer sits beside the
  OpenAlgo bridge.
- Live use stays blocked by the Phase 3 gate, as before.

## Options for the owner

| Option | What happens | Recommendation |
|---|---|---|
| A. Approve this design | POV is ported as intents through the gate, queue, router and one client. | Recommended |
| B. Do not move POV | POV stays in Blue. Stage 6.5 is deferred. Stage 5 (remove Blue) stays blocked. | Only if live POV is not needed |
| C. Port as-is | Not allowed. It breaks the order-path rules. | Rejected |

## Pure parts that do not need this decision

These have no order calls and can be ported after approval, or earlier if you prefer:
- `core/band.py` (target participation rate), `core/liquidity.py`, `core/benchmark.py`,
  `core/lot.py`, `core/time_utils.py`, `core/auction_windows.py`, `core/symbol_meta.py`.
- Sizing and schedule helpers in `pov/slicers/pov.py` that compute quantities only.

## Owner sign-off needed

- Approve option A (or choose B).
- Approve the new client methods (`place_child`, `cancel`) and the live-guard reuse.
- Review the order path in the final port, as with decisions 0018 and 0019.
