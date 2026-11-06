# Dashboard change plan (2026-11-06)

Decision: `docs/decisions/0020-dashboard-api-only-user-limits-kill-switch.md`.
Status: PLAN. No code changed yet.

| ID | Change | Files | Status |
|---|---|---|---|
| D1 | `/` serves JSON API only; remove HTML page from `app.py` | `dashboard/app.py`, `tests/unit/test_dashboard_app.py` | PENDING |
| D2 | Delete `dashboard/assets/terminal.html` | `dashboard/assets/terminal.html` | PENDING |
| D3 | Move `dashboard/index.html` to `src/quant/index.html`; remove simulated engine; keep flow diagram | `src/quant/index.html`, `tests/unit/test_pnl_hidden.py`, SOP, IMPLEMENTATION_PLAN, DASHBOARD_PLAN | PENDING |
| D4 | Risk per trade 0.25% (config default) | `config/risk_limits.yaml` or `config/settings.yaml`, `src/quant/core/config.py` | PENDING (read config first) |
| D5 | Authenticated POST saves user limits to `config/risk_limits.yaml`; audit record; null stays fail-closed | `dashboard/app.py`, `src/quant/audit/`, tests | PENDING |
| D6 | Kill switch closes all open positions, blocks entries (backend) | `src/quant/risk/kill_switch.py`, `Blue_engine/core/kill_switch_store.py` | PENDING (read first) |
| D7 | `risk_used` gets real value from ledger (today always 0.0) | `dashboard/app.py`, `src/quant/reporting/dashboard_data.py` | PENDING |
| D8 | Kill-switch wording in SVG, flow doc, AGENTS rule 8 | `quant_system_flow_v5.svg`, `docs/quant_system_flow_v5.md`, `AGENTS.md` | PENDING (owner confirm: C2PA signature lost) |

Open questions:
1. Kill-switch reading (decision 0020, item 5).
2. SVG C2PA signature loss (decision 0020, item 7).

Test state: not run here (`httpx` and `openalgo` missing). Owner runs `pytest tests/unit tests/parity` after each commit.
