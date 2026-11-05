# Decision 0016: SME lot source, holiday reference, live limits (2026-11-06)

Status: DECIDED by owner. Supersedes item 10 lot-table wording in decision 0015.

## Decisions

1. **SME lot size comes from the broker, not a local table.** OpenAlgo symbol lookup
   (`symbol(symbol=, exchange=)`, field `lotsize`) is read at order time. SME orders use
   whole lots only: qty = lots x lot size. Missing, zero or failed lookup = order refused.
   Non-SME cash equities = 1 share. The local SME table was moved to
   `Temp/Staging Workspace/sme_lot_sizes.csv.unused-2026-11-06` (never delete; AGENTS.md).
   Code: `src/quant/core/expiry_rules.py`. Tests: `tests/unit/test_expiry_lot_rules.py`.
   No order is placed by this change. Order placement stays behind the risk gate (Phase 3, not enabled).

2. **Live risk limits = the paper values.** Owner: "same as paper trade". The same file
   (`config/risk_limits.yaml`) is used. Live trading stays blocked by the Phase 3 gate until
   the other exit checks pass.

3. **Holiday reference CSV.** `config/calendar/nse_holidays_2026.csv` lists every 2026 date
   from the broker equity list and NSE circular CD71962, with source and an `equity_holiday`
   flag (yes / no / disputed). Loader file `config/calendar/holidays.csv` = the 15 equity
   weekday holidays from NSE circular 172/2025 (already cited in the repo). A test keeps the
   two in step.
   - 2026-01-15 (Municipal Corporation Election, Maharashtra): broker lists it as an equity
     holiday; NSE circular 172/2025 does not. Marked `disputed`, not loaded. Owner to confirm.
   - 2026-02-19, 2026-03-19, 2026-04-01, 2026-08-26: in circular CD71962 only; not in the broker
     equity list. Marked `no` pending owner confirmation.
   - 2026-11-08 (Muhurat, Sunday): special session, not a holiday.

4. **Personal use.** Owner confirmed the INFY historical data is for personal use only. The
   source licence allows personal, non-commercial research. The data is not in this repo; the
   owner fetches it on the owner machine with `python Blue_engine/database/sample_data.py`.
   Commercial use needs written permission from the rights holder. Not requested.

5. **Items 13 and 14.**
   - 14 (model registry + human sign-off): the code already exists in
     `src/quant/feedback/champion_challenger.py` and its sign-off test passes in this sandbox.
   - 13 (signal, meta-labeler, regime, factor models): blocked on validation data (X-10). Not built,
     because a model with no out-of-sample test would be unvalidated.
