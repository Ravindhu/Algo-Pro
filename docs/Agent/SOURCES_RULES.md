# External Sources, Feeds & Adapter Rules (`docs/SOURCES_RULES.md`)

## 1. Source Registry & Configuration Rules
- **Single Source Manifest:** Declare all external data feeds, broker APIs, webhook endpoints, and third-party libraries in one centralized configuration manifest; never hardcode URLs, endpoints, or source identifiers inside business logic.
- **Explicit Source Metadata:** Tag every ingested data packet or record with its `source_id`, `ingested_at_utc`, and `sequence_id` so multi-source streams remain traceable and auditable.
- **Version & Contract Pinning:** Pin exact API versions, SDK versions, and schema contracts for every external dependency to prevent silent upstream breaking changes.

## 2. Adapter Isolation & Normalization Rules
- **Anti-Corruption Layer:** Wrap every external feed or API inside an isolated adapter module that translates vendor-specific payloads into internal typed domain models at the system boundary.
- **Zero Vendor Leakage:** Never pass raw third-party JSON payloads, vendor error codes, or proprietary SDK objects into core logic, storage layers, or UI components.
- **Clock & Timezone Normalization:** Convert all external source timestamps into unified UTC epoch/ISO-8601 format immediately upon ingestion before ordering or deduplicating events.

## 3. Reliability, Rate Limits & Failover Rules
- **Rate-Limit & Backoff Compliance:** Enforce per-source token-bucket rate limiters, explicit request timeouts, and jittered exponential backoff on transient `429` or `5xx` errors.
- **Stale Feed & Heartbeat Monitoring:** Track `last_message_utc` per active source; automatically mark a source as `DEGRADED` or `STALE` if silence exceeds its configured heartbeat threshold.
- **Deterministic Deduplication:** Drop duplicate or out-of-order packets from noisy streams using source sequence numbers or idempotency keys before forwarding to the event bus.
- **Graceful Failover:** Support fallback or offline-degraded modes per source so a single unavailable upstream feed never blocks unrelated system components.Reject data older than its allowed age before any module uses it. Log the rejection."
## 4. Licensing, Compliance & Attribution Rules
- **Dependency & Data License Audit:** Verify and log the license terms (e.g., MIT, Apache-2.0, commercial feed restrictions) of every external repository, dataset, or API integrated into the project.
- **Secret & Credential Segregation:** Isolate authentication tokens, API keys, and webhook signing secrets per source inside `.env` variables, rotating or refreshing expired session tokens inside the adapter without restarting the host process.
