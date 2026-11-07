# 0030. Flow popup shows live node status

Date: 2026-11-07. Status: accepted (owner request).

## Context
Decision 0020 made the FLOW popup a static diagram. The owner wants each node of the v5 flow to show its
current state, and a node with an error to blink red.

## Decision
- `/api/overview` gains `flow`: a map of node label to `{status, message}` for nodes that need attention.
  Built by `src/quant/reporting/flow_status.py` from current state only (kill switch, mode, loop error,
  calendar, feed, unset limits). Old audit events never colour a node.
- Status `err` blinks red. Status `warn` gets an amber border. Nodes not in the map are ok.
- The popup shows the diagram with colours, a tooltip per node, and a list of flagged nodes.
- If the API cannot be reached, the `Dashboard` node turns red.
- No new route. Read-only: the popup still sends no orders and adds no POST.

## Consequences
- Feed nodes stay amber until a live feed exists. This is the truth, not an error.
- Node labels must match `quant_system_flow_v5.svg` boxes. `tests/unit/test_flow_status.py` checks this.
- Plan: `docs/FLOW_NODE_STATUS_PLAN_2026-11-07.md`.
