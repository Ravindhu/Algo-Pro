# 0028 - Paper and live mode on the dashboard

Date: 2026-11-07
Status: ACCEPTED (owner request, 2026-11-07). Live stays refused.

## Context

The owner asked for PAPER and LIVE buttons in the dashboard header, wired to the backend.
Live orders are guarded (decision 0017): a LiveApproval with an approver, a matching limits
hash, an approved GateDecision and an API key are all required. The dashboard does not create them.

## Decision

1. The header has PAPER and LIVE. The page shows the mode that the backend reports.
2. `GET /api/trading-mode` returns the mode and `live_allowed: false`.
3. `POST /api/trading-mode` (token) accepts `paper`. It refuses `live` with HTTP 409 and the reason.
   Any other value is refused with HTTP 422. Every request is written to the audit log.
4. The mode is not saved to a file. Paper is the only mode the dashboard can select, so a restart returns to paper.
5. Allowing live is a separate decision. It needs the live guard flow in decision 0017, not a dashboard switch.

## Open items

- START and STOP (background paper loop) are still open. See the chat for the options.
