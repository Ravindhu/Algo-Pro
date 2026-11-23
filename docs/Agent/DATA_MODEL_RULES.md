# Data Modeling & Type Safety Rules (`docs/DATA_MODEL_RULES.md`)

## 1. Schema Definition & Single Source of Truth
- **Typed Domain Models:** Define all entities, payloads, and state objects using strict static schemas (e.g., Pydantic `BaseModel`, `dataclasses`, or TypeScript interfaces); strictly forbid passing untyped `dict` or `any` objects across module boundaries.
- **Boundary Validation:** Validate and coerce all external inputs (API requests, WebSocket frames, webhook payloads, config files) at system entry points before they enter core domain logic.
- **Immutability by Default:** Mark value objects, events, and read-only state snapshots as immutable (`frozen=True` / `readonly`) so downstream consumers cannot mutate shared references mid-execution.

## 2. Field Precision, Units & Naming Standards
- **Exact Numeric Types:** Represent monetary values, prices, rates, and quantities using exact decimal types (`Decimal`) rather than binary floats (`float`) to prevent rounding drift.
- **Explicit Units in Field Names:** Suffix ambiguous numeric fields with their exact unit (`_ms`, `_pct`, `_qty`, `_bps`, `_utc`) so scale and unit conversions are unmistakable.
- **Strict Enums Over Magic Strings:** Model all finite states, sides, modes, and statuses using explicit `Enum` or union literal types; never scatter raw status strings across conditionals.
- **Nullable Explicitness:** Declare optional fields explicitly (`Optional[T] = None`) and separate "missing/unset" (`None`) from valid zero/empty values (`0`, `""`).
       Round numbers only at the display layer or at a named, documented boundary. Never round inside calculations."
## 3. State Lifecycle & Serialization Rules
- **Enforced State Machines:** Define valid state transitions explicitly inside the model or state manager and reject illegal transitions with deterministic domain errors.
- **Separate DTOs from Domain Entities:** Keep transport schemas (API request/response DTOs) and persistence models decoupled from internal domain entities via explicit mapper functions.
- **Deterministic Serialization:** Standardize JSON serialization rules across all models (ISO-8601 UTC strings for timestamps, string-encoded decimals where precision must survive JSON transport, and consistent `snake_case` or `camelCase` conventions).
- **Schema Versioning:** Include an explicit `schema_version` field on persisted state snapshots, exported configs, and WebSocket envelopes to support backward-compatible upgrades.
