# src/quant/execution

Orders. Phase 3 only. Nothing here reaches OpenAlgo in paper mode.

- `order_fsm.py`: order state machine. Retry-safe (X-3). DONE for retry.
- `queue.py`: approved-order queue. Refuses unapproved intents. PARTIAL.
- `openalgo_client.py`: the only file allowed to call the order API. Refuses in paper mode. Has `place`, `place_child` (MARKET/LIMIT) and `cancel`, all behind the live guard. PARTIAL.
- `readonly_client.py`: quotes, depth, orderbook. No order methods (decision 0021). DONE.
- `lot.py`: lot-size rounding for child orders (pure port of Blue core/lot.py; lookup injected). DONE (decision 0022).
- `pov_executor.py`: child orders: child intent -> gate -> queue -> `place_child`, with audit. Skeleton; POV loop not ported yet. DONE (skeleton).
- `router.py`, `protection.py`: algo router, stop at fill. PENDING.

Rule: `scripts/run_live.py` is NOT created until Phase 3 exit checks pass.
See `docs/IMPLEMENTATION_WORKFLOW.md` Step 7.
