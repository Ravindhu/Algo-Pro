# API & Real-Time Transport Rules (`docs/API_RULES.md`)

## 1. Transport & Contract Rules
- **Versioned REST Namespace:** Prefix all HTTP routes consistently under `/api/v1` and enforce strict request/response schema validation (e.g., Pydantic) on every endpoint.
- **Unified JSON Envelope:** Return deterministic JSON structures for all responses; never return raw HTML error pages or unhandled stack traces to the client.
- **Standardized Error Schema:** Format every non-2xx response with `{ "status": "error", "code": "<MACHINE_CODE>", "message": "<READABLE_REASON>", "retryable": <boolean> }`.
- **WebSocket Event Envelope:** Structure every real-time push message uniformly as `{ "type": "<EVENT_TYPE>", "ts": "<ISO_8601_TIMESTAMP>", "data": { ... } }`.

## 2. Resilience & Offline Degradation Rules
- **No Cascading 503 Crashes:** If an upstream broker, market feed, or external service disconnects, return a structured degraded state rather than failing health or state endpoints with unhandled `503` errors.
- **Heartbeat & Stale Detection:** Emit periodic ping/heartbeat events on active WebSocket connections so clients can detect stale feeds within `5s` and auto-reconnect with exponential backoff.
- **Idempotent State Mutations:** Ensure control, toggle, and order-modifying endpoints safely handle duplicate retries without triggering double executions.

## 3. Concurrency & Performance Rules
- **Non-Blocking Execution:** Run HTTP servers and WebSocket broadcast loops asynchronously; never allow slow UI clients or polling requests to block core engine tick ingestion or order routing.
- **Delta & Throttled Streaming:** Push only changed state deltas over WebSockets and throttle high-frequency tick broadcasts (`100ms–500ms` batching) to prevent frontend queue saturation.
- **Strict Timeouts:** Enforce explicit connect and read timeouts on all outbound HTTP calls to external brokers or automation webhooks.

## 4. Security & Validation Guardrails
- **Credential Masking:** Load secrets exclusively from environment variables (`.env`); never return raw API keys, tokens, or passwords in `GET` configuration responses (mask all sensitive fields).
- **Local Origin Lockdown:** Restrict CORS policies strictly to local dashboard origins (`localhost` / `127.0.0.1`) unless explicitly configured otherwise.
- **Pre-Execution Risk Gate:** Validate every order-placing or parameter-updating request against server-side risk bounds (max quantity, max daily loss, allowed symbols) before passing to the execution layer.
