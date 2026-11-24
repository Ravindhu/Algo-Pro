# Implementation Plan – Owner Request 2026-11-24 (status: PLANNING, no code changed)

Simple-English plan. Live trading stays OFF. Nothing here changes order behaviour until the owner confirms the open questions.

## What the owner asked

1. Dashboard: only admin accounts needed; remove the view-only role from the UI.
2. Start OpenAlgo and the broker login automatically (with TOTP) when the engine starts.
3. Dashboard "Infrastructure" header: enter the `.env` values and save them from the dashboard.
4. Risk profiles: wider aggressive and conservative ranges; owner can set a higher aggressive risk %.
5. Unfilled shares, and unfilled risk, carry into the next cycle (OCL).

## What the code already does (read-only check)

| # | Item | Found in code | Gap |
|---|------|---------------|-----|
| 1 | Admin-only dashboard | `core/dashboard_accounts.py`: roles `owner` / `viewer`; self-signup is always `viewer` | UI still shows viewer accounts; need to decide what to remove |
| 2 | Auto broker login | `engines/core/broker_auth.py`: headless TOTP login, opt-in via `BROKER_AUTOLOGIN=1`; TOTP never stored | Not clear if it runs on engine start. Auto-login with secrets is a live-trading security decision (see Q2) |
| 3 | `.env` from dashboard | `core/env.py` loads `<repo>/.env` at start; no dashboard writer | New write path for secrets: must mask values, never log, owner-only |
| 4 | Wider risk ranges | `strategies/risk_tree.py`: `aggressive_share` allowed 0.50–0.80 (default 0.60); `strategies/risk_ladder.py`: fixed probe 0.10 % + 0.15 % | Ranges are hard-coded limits. Need owner values (see Q4) |
| 5 | Carry-over | `strategies/risk_ladder.py` (`carry`, `closed`), `execution/strategy_bot.py` (unfilled carried to next HTF OCL; cap-blocked shares carried) | Looks already built. Need the exact failing case (see Q5) |

## Phases (proposed, after owner answers)

- **P0 – Answers:** owner answers Q1–Q5 below. No code.
- **P1 – Risk ranges (item 4):** new bounds in config (not in code), dashboard fields, validation, tests for min/max and "never overrides hard risk limits".
- **P2 – Carry-over check (item 5):** write a failing regression test for the case the owner describes; fix only if the test fails.
- **P3 – `.env` editor (item 3):** owner-only endpoint, masked display, atomic write, no secret in logs, audit row per save. Tests: secret never returned in GET, never logged.
- **P4 – Admin-only UI (item 1):** hide viewer management; keep server check.
- **P5 – Auto login on start (item 2):** only if owner approves Q2. Tests with mocked broker.
- **P6 – Verify:** run affected tests, secret-leak check, then commit and push.

## Open questions for the owner

- **Q1:** Item 1 – delete existing viewer accounts, or only hide them in the UI?
- **Q2:** Item 2 – auto-login on engine start: should it also run when live is ON? (Recommended: auto-login for paper only; live needs a manual approval click.)
- **Q3:** Item 3 – may the `.env` editor save broker secrets and TOTP secrets from the browser? (Recommended: yes, owner-only, masked, audit logged.)
- **Q4:** Item 4 – exact new ranges: aggressive %, conservative %, and the max aggressive risk % you want to allow.
- **Q5:** Item 5 – describe one case where unfilled shares or risk were NOT carried (which strategy, which cycle).

## Owner answers (2026-11-24)

- **Q1 (item 1):** Delete viewer accounts. Status: approved, not implemented.
- **Q2 (item 2):** Auto-login OpenAlgo and the broker with 2FA. The dashboard login stays manual. Status: approved for paper. Live auto-login is NOT confirmed; recommendation is a manual approval click for live.
- **Q3 (item 3):** Yes. The owner saves the broker password and TOTP secret once. At each dashboard sign-in, OpenAlgo and the broker log in automatically. Recommendation: store secrets encrypted at rest, owner-only, masked in the UI, never logged.
- **Q4 (item 4):** Per-level risk range 0–100 % with no guard. Conflict to confirm: `quant-rule.md` section 4 says hard risk limits must not be overridable in code. Recommendation: keep the existing daily loss cap (2 %) and kill switch as the hard limit, even though the per-level % is free.
- **Q5 (item 5):** Recommendation only, not yet implemented. Problem: an aggressive entry of 0.15 % risk rounds to 0 shares.
  - Keep the unused rupee risk and carry it to the next entry in the same OCL, not to the next share count.
  - If still 0 shares when the OCL closes, drop it and write an audit row with the reason. Do not round up to 1 share, because that breaks the risk limit.
  - Add a test: 0-share entry carries risk; a later entry that fits uses the carried risk.

## Owner decisions (2026-11-24, second round)

- **Item 4 (risk range):** SKIPPED. Keep the current ranges unchanged. No code change.
- **Item 5 (0-share entries):** Recommendation APPROVED. Carry unused rupee risk to the next entry in the same OCL; drop with an audit row if still 0 shares at OCL close; never round up to 1 share.
- **New request (dashboard):** Show only one control on the dashboard: a bot ON/OFF button. Scope to confirm: which other dashboard controls (including the kill switch) are removed or moved.
- **Live auto-login (earlier question):** NOT answered clearly. Still open.

## Owner decisions (2026-11-24, third round) – all questions now answered

- **Dashboard buttons:** One main button: BOT START / BOT STOP. It replaces the separate Start Engine and Stop buttons. Keep RESET as its own control. Interpretation to confirm in the UI review: the kill switch is treated as the STOP action.
- **Live auto-login:** YES. OpenAlgo and the broker log in automatically in live mode too (no manual approval click). This overrides the earlier recommendation. Secrets must be encrypted at rest and never logged.
