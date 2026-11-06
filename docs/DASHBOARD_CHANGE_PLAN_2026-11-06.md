# Dashboard change plan (2026-11-06)

Decision: `docs/decisions/0020-dashboard-api-only-user-limits-kill-switch.md`.
Status: IN PROGRESS. D1, D2, D6 and the flow-doc part of D8 are done in the commit that adds `src/quant/risk/kill_close.py`.

| ID | Change | Files | Status |
|---|---|---|---|
| D1 | `/` serves JSON API only; HTML page removed from `app.py` | `dashboard/app.py`, `tests/unit/test_dashboard_app.py` | DONE (FastAPI tests skip here; owner run needed) |
| D2 | Delete `dashboard/assets/terminal.html` | `dashboard/assets/terminal.html` | DONE (deleted, not moved) |
| D3 | Move `dashboard/index.html` to `src/quant/index.html`; remove simulated engine; keep flow diagram | `src/quant/index.html`, `tests/unit/test_pnl_hidden.py`, SOP, IMPLEMENTATION_PLAN, DASHBOARD_PLAN | PENDING |
| D4 | Risk per trade 0.25% (config default) | `config/risk_limits.yaml` or `config/settings.yaml`, `src/quant/core/config.py` | PENDING (read config first) |
| D5 | Authenticated POST saves user limits to `config/risk_limits.yaml`; audit record; null stays fail-closed | `dashboard/app.py`, `src/quant/audit/`, tests | PENDING |
| D6 | Kill switch closes all open positions, blocks entries (backend) | `src/quant/risk/kill_close.py`, `scripts/run_paper.py`, `src/quant/reporting/dashboard_data.py`, `tests/unit/test_kill_close.py` | DONE for the paper runner. Automatic triggers are not wired yet (no production code calls `KillSwitch.trip`). Halt-all safe mode and `risk_limits.blocked` still stop exits (existing policy) |
| D7 | `risk_used` gets real value from ledger (today always 0.0) | `dashboard/app.py`, `src/quant/reporting/dashboard_data.py` | PENDING |
| D8 | Kill-switch wording in SVG, flow doc, AGENTS rule 8 | `quant_system_flow_v5.svg`, `docs/quant_system_flow_v5.md`, `AGENTS.md` | Flow doc DONE. SVG and AGENTS PENDING (owner confirm: C2PA signature lost) |

Open questions:
1. Kill-switch reading (decision 0020, item 5): backend closes positions; the page shows the close. Confirm.
2. SVG C2PA signature loss (decision 0020, item 7). The SVG still says "blocks entries" until this is answered.

Test state (sandbox, this commit): `pytest tests/unit tests/parity` = 27 failed, 1722 passed, 15 skipped, 1 xfailed. All 27 failures are the HRP / `httpx` set that also fails on the untouched commit. The FastAPI tests are skipped here because `fastapi` is not installed. Owner runs the full suite on the owner machine.
