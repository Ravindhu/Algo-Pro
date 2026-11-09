# 0030 — Dashboard LIVE mode, locked behind a LiveApproval

Date: 2026-11-08
Status: ACCEPTED by owner ("option A", 2026-11-08). Supersedes decision 0028 point 5.

## Context

Decision 0028 gave the dashboard PAPER and LIVE buttons but hard-refused LIVE, with the
open item "Allowing live is a separate decision". The owner asked for the switch to work:
"if i change the paper to live it should change live also in nse dashboard".

The Phase 3 gate is still NOT cleared:

- `config/risk_limits.yaml` live values exist (decision 0016: live = paper) but no
  `LiveApproval` has ever been recorded against them.
- Phase 3 exit check 2 (SEBI algo registration, order tagging, NTP sync) is NOT verified.
- Phase 3 exit check 4 (kill switch and safe-mode drill, no orders sent) has NOT been run.

So a dashboard switch that simply flips a boolean into live mode would place real orders
with real money on a mis-click, with none of those checks cleared. That is the failure
this decision exists to prevent.

## Decision — "armed but disarmed"

The switch exists, the live runner exists, and **neither can place an order without a
human-signed approval file**.

1. **Mode is state, not authority.** `core/trading_mode.py` persists the requested mode to
   `data/mode.json`. The mode says what the operator WANTS. It never authorises an order.
2. **LIVE requires a LiveApproval.** `POST /api/trading-mode {"mode":"live"}` is accepted
   only when `data/live_approval.json` exists, names an approver, and its
   `limits_sha256` matches the current `config/risk_limits.yaml`. Otherwise HTTP 409 with
   the exact reason.
3. **The approval is created by a person, never by the dashboard.** It is written by
   `scripts/ops/approve_live.py`, an interactive command that shows the limits and requires
   the operator to type the approval phrase. No API route creates or edits it.
4. **A changed limits file voids the approval.** Editing any limit changes the hash, so the
   mode falls back to paper and the dashboard says why. Same rule the execution guard
   already enforces (`openalgo_client._check_live_session`).
5. **`run_live.py` re-checks everything at start and per order.** The runner refuses to
   start without a valid approval, a cleared kill switch, a verified calendar, and no
   `blocked: true`. It passes a real `LiveApproval` to `OpenAlgoClient` in `live` mode, so
   the order path still needs an approved `GateDecision` with a matching `intent_id`.
6. **Reverting is instant and one-way-safe.** `{"mode":"paper"}` always succeeds without
   approval. A restart re-reads the file and re-validates, so a stale approval never
   silently re-arms the system.

## What this does NOT do

- It does not clear Phase 3 exit checks 2 or 4. Those stay open and are listed in
  `docs/IMPLEMENTATION_PLAN.md`.
- It does not make SEBI compliance true. That needs verification from official SEBI
  sources and an owner sign-off.
- It does not let any automated process promote, size or place without the gate.

## Consequences

- The dashboard reports the real mode, so paper and live can no longer drift apart: one
  source (`data/mode.json`) drives the header, the loop and the runner.
- The risk of a mis-click is bounded: switching to LIVE without an approval does nothing
  except write an audit row.
- The operator now has one extra deliberate step before real money: run the approval
  script. That step is the point.
