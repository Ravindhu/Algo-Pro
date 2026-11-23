# Automated Testing & Verification Rules (`docs/TESTING_RULES.md`)

## 1. Test Pyramid & Scope Discipline
- **Unit Tests (Core Domain):** Test all pure calculations, risk checks, state machine transitions, and data mappers in complete isolation with zero network, disk, or external broker dependencies.
- **Integration Tests (Adapters & API):** Verify REST endpoints, WebSocket event streams, and database persistence layers against ephemeral in-memory or temporary SQLite databases (`tmp_path`).
- **End-to-End Smoke Tests:** Maintain a fast smoke suite covering critical user flows (startup, degraded offline state handling, config updates, and emergency stop triggers).

## 2. Determinism & Isolation Rules
- **Zero Live Network Calls:** Strictly forbid hitting live broker APIs, external webhooks, or production endpoints inside automated test runs; mock all external adapters at the boundary interface.
- **Deterministic Clocks & Randomness:** Never rely on live system clocks (`datetime.now()`), `time.sleep()`, or unseeded random generators in tests; inject controllable clocks/timers and fixed seeds.
- **Stateless Test Execution:** Every test must initialize fresh state and clean up after itself so tests pass in any execution order (`pytest-randomly` safe).

## 3. Edge Cases, Concurrency & Failure Testing
- **Mandatory Boundary Coverage:** Explicitly test zero values, negative numbers, `None`/missing fields, precision rounding boundaries, and empty collections.
- **Fault & Degradation Injection:** Simulate upstream `503` outages, malformed payloads, stale feed timeouts, and duplicate/retry deliveries to verify graceful error envelopes and idempotency.
- **Async & Race Condition Checks:** Test concurrent state reads/writes and WebSocket broadcast loops under simulated load to ensure zero deadlocks or state corruption.
- **Fail-closed test:** when a safety check errors or times out, the action must be blocked, and a test must prove it
## 4. Test Structure & Execution Standards
- **Arrange-Act-Assert (AAA):** Structure every test clearly into setup, single action, and explicit assertions with descriptive failure messages.
- **Regression Lock:** Every bug fix must include a failing regression test reproducing the exact defect before applying the patch.
- **Fast Execution Gate:** Keep the core unit and integration suite fast (`< 10s`) and run affected tests automatically after every code change before marking a task complete.
