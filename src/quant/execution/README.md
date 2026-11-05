# src/quant/execution

Orders. Phase 3 only. Nothing here reaches OpenAlgo in paper mode.

- `order_fsm.py`: order state machine. Retry-safe (X-3). DONE for retry.
- `queue.py`: approved-order queue. Refuses unapproved intents. PARTIAL.
- `openalgo_client.py`: the only file allowed to call the order API. Refuses in paper mode. PARTIAL.
- `router.py`, `protection.py`: algo router, stop at fill. PENDING.

Rule: `scripts/run_live.py` is NOT created until Phase 3 exit checks pass.
See `docs/IMPLEMENTATION_WORKFLOW.md` Step 7.
