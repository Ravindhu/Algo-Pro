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

3. **Holiday source = NSE circulars (owner instruction, 2026-11-06).**
   - Base list: NSE/CMTR/71775 (12 Dec 2025), Capital Market holidays 2026: 15 weekday holidays.
   - Amendment: NSE circular 07/2026 (12 Jan 2026) declares 2026-01-15 (Municipal Corporation
     Election, Maharashtra) a Capital Market holiday. Official list = 16 weekday holidays.
   - Not holidays: 2026-02-19, 2026-03-19, 2026-04-01, 2026-08-26. They appear only in NSE
     circular CD71962, which does not name a segment, and not in the Capital Market list.
   - 2026-11-08 (Muhurat, Sunday): special session, not a holiday.
   - `config/calendar/holidays.csv` (loader) = the 16 official weekday holidays.
   - `config/calendar/nse_holidays_2026.csv` = full reference with source and equity flag per row.
     Test `test_loader_csv_equals_reference_equity_rows` keeps the two in step.

4. **Personal use.** Owner confirmed the INFY historical data is for personal use only. The
   source licence allows personal, non-commercial research. The data is not in this repo; the
   owner fetches it on the owner machine with `python Blue_engine/database/sample_data.py`.
   Commercial use needs written permission from the rights holder. Not requested.

5. **Items 13 and 14.**
   - 14 (model registry + human sign-off): the code already exists in
     `src/quant/feedback/champion_challenger.py` and its sign-off test passes in this sandbox.
   - 13 (signal, meta-labeler, regime, factor models): blocked on validation data (X-10). Not built,
     because a model with no out-of-sample test would be unvalidated.
