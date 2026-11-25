# Plan: remove LiveApproval and the egress IP requirement (2026-11-25)

Owner decision (2026-11-25): option 1. Remove the LiveApproval mechanism and the egress IP
requirement. OpenAlgo's analyzer switch becomes the single live/sandbox control.

## Rule after this change
- OpenAlgo analyzer ON (sandbox): every order is refused, even when the bot is set to live.
  Orders are sent only in analyze mode (sandbox confirmed on).
- OpenAlgo analyzer OFF: live orders may go to the broker, if the compliance gate passes
  (audit storage and clock, unchanged) and the risk gate approves each intent (unchanged).
- Analyzer status unclear (no answer, bad answer): refused (fail closed).

## Phases
1. OpenAlgo client (src/quant/execution/openalgo_client.py): remove LiveApproval, file_sha256,
   the limits-hash check and the live_approval / limits_path arguments. Live guard = analyzer
   must be off + compliance. Analyze guard unchanged.
2. Mode switch (src/quant/core/trading_mode.py): no approval file. Mode is saved as before.
   Live selection is accepted and audited; sending is still gated by step 1.
3. Start checks (scripts/run/run_live.py startup_checks) and live bot runner
   (scripts/run/run_strategy_bot_live.py): remove the approval steps. Egress IP: already not
   enforced in code on master (comment only); comment kept, nothing added.
4. Dashboard (dashboard/app.py): drop the approval file path; keep the mode switch.
5. Delete src/quant/core/live_approval.py, scripts/ops/approve_live.py and their tests.
   Update tests that depended on approval.
6. Docs: config/bot.yaml header comment, strategy_bot.py docstring.

## Not changed
- Risk gate, kill switch, daily loss caps, compliance gate, broker stops, paper mode.
- Live stays off in config: live_enabled and mode are not changed by this plan.

## Checks
- Full test suite before and after (master baseline: 4 failures, pre-existing, see notes).
- New test: live order refused when analyzer is on; allowed when analyzer is off.
