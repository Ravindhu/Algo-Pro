# System Architecture & Engineering Rules (`docs/ARCHITECTURE_RULES.md`)

## 1. Layer Separation & Dependency Direction
- **Strict Layered Boundaries:** Enforce one-way dependencies across four isolated layers:
  1. **Domain / Core Logic:** Pure deterministic calculations, rule evaluations, and state transitions with zero framework, HTTP, or UI imports.
  2. **Adapters / Connectors:** Isolated wrappers for external brokers, data feeds, webhooks, and databases implementing uniform internal interfaces.
  3. **API / Transport Layer:** Thin REST and WebSocket controllers responsible strictly for schema validation, serialization, and delegating to application services.
  4. **Presentation / UI Layer:** Stateless visual components consuming normalized state from a single reactive client store.
- **No Circular Imports:** Modules must form a strict Directed Acyclic Graph (DAG); shared types and schemas live in a dedicated core contracts module.

## 2. State Management & Data Flow
- **Single Source of Truth:** Maintain one authoritative in-memory state manager for active runtime state, backed by durable persistence for audit trails and crash recovery.
- **Event-Driven Decoupling:** Communicate between ingestion feeds, evaluation engines, order routers, and UI broadcasters via an internal async event bus or typed queues rather than tight direct coupling.
- **Immutable State Snapshots:** Expose read-only state snapshots to transport and UI layers so external readers cannot mutate live engine state mid-cycle.

## 3. Concurrency, Fault Isolation & Recovery
- **Isolate Critical Execution Paths:** Keep latency-sensitive ingestion and execution loops completely decoupled from background tasks (logging, persistence writes, UI WebSocket broadcasts).
- **Bulkhead Fault Containment:** Wrap external adapter calls in circuit breakers and retry boundaries so a single failing feed or webhook never crashes the core runtime loop.
- **Crash-Safe Persistence:** Use atomic writes or WAL-mode local storage for critical state transitions so restarting the process cleanly restores open state without duplicates.
- **Graceful Shutdown:** Handle termination signals (`SIGINT` / `SIGTERM`) by flushing pending writes, closing active sockets, and halting new triggers cleanly.

## 4. Modularity & Extensibility Rules
- **Plugin / Strategy Interface:** Every strategy, scanner, or automation rule must implement a uniform lifecycle interface (`init`, `on_tick`/`on_event`, `teardown`) so modules can be toggled or hot-reloaded without modifying core engine code.
- **Configuration Over Hardcoding:** Drive environment-specific parameters, thresholds, and feature flags from external config files and `.env` schemas validated at startup.
- **File & Module Cohesion:** Keep each file focused on one single responsibility and under ~300 lines; split growing modules by domain capability rather than technical file type.
Safe defaults: any risky feature is off unless explicitly enabled in config."
   "Refuse to start if required config is missing or invalid. Log the exact reason."
