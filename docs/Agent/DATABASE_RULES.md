# Database & Persistence Rules (`docs/DATABASE_RULES.md`)

## 1. Engine Configuration & Concurrency
- **WAL Mode & Busy Timeout:** For embedded databases (SQLite), always enable Write-Ahead Logging (`PRAGMA journal_mode=WAL;`), `PRAGMA synchronous=NORMAL;`, and a non-zero `busy_timeout` (`>= 5000ms`) on every connection to prevent write-lock contention during concurrent reads.
- **Connection Pooling & Lifecycle:** Manage database sessions through centralized context managers or async connection pools; never open raw unclosed connections inside request handlers or tight execution loops.
- **Non-Blocking Persistence:** Offload high-frequency log, tick, and audit writes to a batched async background writer queue so disk I/O never blocks real-time execution paths.

## 2. Schema Design & Data Types
- **Explicit Migrations:** Manage all schema creation and alterations through versioned, idempotent migration scripts; never rely on destructive table drops in runtime startup code.
- **Numeric Precision:** Never store financial amounts, prices, quantities, or percentages as floating-point (`FLOAT`/`REAL`) types; use exact fixed-point decimals (`NUMERIC`/`DECIMAL`) or scaled integer minor units.
- **Standardized Timestamps:** Store all timestamps in UTC using ISO-8601 strings or integer epoch milliseconds (`created_at`, `updated_at`), converting to local display timezones strictly in the presentation layer.
- **Referential & Check Constraints:** Enforce foreign keys (`PRAGMA foreign_keys=ON;`), `NOT NULL` defaults, and `CHECK` constraints at the database level rather than relying solely on application-side validation.

## 3. Indexing & Query Performance
- **Index Filter & Sort Columns:** Create composite indexes on all columns frequently used in `WHERE`, `JOIN`, and `ORDER BY` clauses (especially timestamp + status/symbol lookups).
- **No Unbounded Queries:** Every `SELECT` query feeding UI tables, logs, or API endpoints must enforce explicit `LIMIT` and pagination/cursor bounds.
- **Parameterized Queries Only:** Always use bound query parameters (`?` or named placeholders); strictly forbid string interpolation or concatenation in SQL statements.

## 4. Auditability, Retention & Backup Rules
- **Append-Only Audit Trails:** Treat execution logs, state transitions, and order events as immutable append-only records; never overwrite or hard-delete historical audit rows (use soft-delete flags or status columns where needed).
- **Idempotency Keys:** Enforce `UNIQUE` constraints on external event IDs, order IDs, and webhook delivery IDs so retries never create duplicate records.
- **Automated Retention & Backups:** Implement scheduled log compaction/archival for high-volume telemetry tables and atomic online backups (`.backup` API) without halting active readers.
