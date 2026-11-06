# 0026 - Analyzer mode path (PROPOSED, owner to approve)

Date: 2026-11-06
Status: PROPOSED. Code is written behind this decision; owner sign-off is still needed.

## Context

The pipeline has a paper runner (in-process PaperBroker) but no path through OpenAlgo that
stays in the sandbox. OpenAlgo's analyzer mode gives that: placeorder is simulated when the
analyzer is on.

## Decision

1. OpenAlgoClient gets a mode "analyze". It sends to the same placeorder path, but only after
   POST /api/v1/analyzer confirms analyze_mode is true (fail closed).
2. Analyze mode needs an approved GateDecision and an API key. It does not need LiveApproval.
3. Live mode and its guard are unchanged. Paper mode stays refused by the client.
4. Fills are recorded only from order status "complete" with a positive average price.
5. Residual risk (analyzer switched off between check and send) is accepted for the sandbox and
   listed in docs/ANALYZER_MODE_PLAN.md section 3 (A5).

## Owner question

Approve the analyzer check, or ask for a stricter rule (for example, a check that the
analyzer is on at session start and at every send, or a sandbox-only flag in config).
