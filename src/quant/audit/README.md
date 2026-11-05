# src/quant/audit

Audit trail for signals, orders, executions and risk decisions.

- `override_log.py`: actor and reason rule tested. DONE.
- `log.py`: append-only audit log. Tamper test passes. PARTIAL. The immutable-store check is part of the SEBI checklist (P3-8; scope question open).

Rule: every risk decision writes a record, both pass and block.
