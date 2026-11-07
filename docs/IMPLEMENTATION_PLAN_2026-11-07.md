# Implementation plan, 2026-11-07

Plain-English plan for this batch. Paper and research only. Nothing here sends an order.

## Done in this batch
| Item | What | Where | Test |
|---|---|---|---|
| 4 | Feed reconnect with exponential backoff (pure schedule; connect and sleep are injected) | `src/quant/data/reconnect.py` | `tests/unit/test_reconnect.py` |
| 11 | Reconciliation job: ledger vs broker positions, logged to the audit log | `src/quant/monitor/recon_job.py` | `tests/unit/test_recon_job.py` |
| 17 | Startup clock-skew check against NTP, logged; skew blocks trading (fail closed) | `src/quant/monitor/clock.py` | `tests/unit/test_clock.py` |
| 18 | Backup of ledger, audit log and limits with SHA-256 manifest; restore refuses to overwrite and checks hashes | `src/quant/monitor/backup.py` | `tests/unit/test_backup_restore.py` |
| 19 | CI: GitHub Actions runs `pytest tests/unit tests/parity` on push and pull request | `.github/workflows/tests.yml` | runs in GitHub |
| 22 | Strategy-deviation alert: CUSUM drift alarm emits an ERROR alert | `src/quant/monitor/strategy_health.py` | `tests/unit/test_deviation_alert.py` |

## Not done in this batch (needs an answer first)
| Item | Why not yet |
|---|---|
| 1 Demerger | Needs the demerger ratio convention for back-adjustment. Confirm the rule. |
| 3 Delivery calendar | Delivery has no expiry and uses the same holiday list. Unclear what to add. Please say what "for delivery trading" should change. |
| 6 Square-off retry | Goes through the order path. Needs the gate design approved. |
| 7 Circuit freeze wired to feed | Needs the halt event source from the broker feed. |
| 9 Pre-open state | Needs the exact NSE pre-open times from the circular. |
| 10 T+1 funds in gate | Changes the risk gate. Needs approval. |
| 13 SEBI algo tag | I cannot confirm the SEBI rule. Owner must confirm the rule. |
| 15 Audit off-machine copy | Owner decision on where copies go. |
| 23 Hedge overlay | Deferred by owner instruction. |
| 24 Order-rate caps | Exchange cap value comes from the broker. Needs the broker value. |

## Scheduling not done here
- Nightly backup, reconciliation and clock check need an OS task (Windows Task Scheduler or cron) on the owner machine. This batch provides the functions only.
- The reconciliation job needs a read-only broker positions reader wired in. This batch takes it as an argument.

## Rules followed
- Each new file has a `Flow stage:` line and a `Status:` line (checked by `tests/unit/test_layout.py`).
- No new code calls the order API.
- Tests run in the sandbox without network. Unit tests use fakes.
