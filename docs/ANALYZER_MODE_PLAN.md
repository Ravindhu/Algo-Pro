# Analyzer mode end-to-end plan

Status: IN PROGRESS (created 2026-11-06). Owner decision needed: docs/decisions/0026-analyzer-mode-path.md.

## 1. Goal

The quant pipeline runs end to end against OpenAlgo in analyzer mode:
signals -> risk gate -> approved queue -> OpenAlgo placeorder (sandbox) -> order status -> ledger -> audit.

Analyzer mode is OpenAlgo's server-side sandbox. When it is on, placeorder is simulated and
no real broker order is sent. The client must confirm this before every send.

## 2. What analyzer mode is NOT

- Not live trading. Live still needs LiveApproval + GateDecision + API key (decision 0017).
- Not the in-process PaperBroker. Paper stays as it is. Analyze goes through OpenAlgo.

## 3. Safety rules (fail closed)

- A1. Analyze mode sends only if POST /api/v1/analyzer returns success with data.analyze_mode == true.
      Any error, missing field or false value -> refuse, nothing sent.
- A2. Analyze mode still needs an approved GateDecision for the same intent id.
- A3. Analyze mode needs no LiveApproval (no real money), but it never changes the live guard.
- A4. Fill is recorded only when order status is "complete" and average_price is a positive number.
      Anything else -> status UNKNOWN, no ledger fill, audit line written.
- A5. Residual risk: OpenAlgo analyzer could be switched off between the check and the send.
      Mitigation: check immediately before send; owner runs with analyzer switched on only in the
      sandbox session. Recorded here, not hidden.

## 4. Phases

### AZ1 - Client guard (code)
- AZ1-1. OpenAlgoClient accepts mode "analyze" besides "live". "paper" stays refused.
- AZ1-2. `require_analyzer_on(api_key)` check before place/place_child/cancel in analyze mode.
- AZ1-3. `get_order_status(api_key, orderid, strategy)` read path (order client module only).
- AZ1-4. `http_transport(url, payload)` stdlib JSON POST, no new dependency.
Tests: tests/unit/test_analyze_mode.py (fake transport, every refusal path, the allowed path).

### AZ2 - Runner (code)
- AZ2-1. run_paper.run takes a broker object; default stays PaperBroker.
- AZ2-2. AnalyzeBroker: place(intent, decision) -> send, then order status -> FILLED / REJECTED / UNKNOWN.
- AZ2-3. run_paper handles non-FILLED results: no ledger fill, no exposure change, audit line.
- AZ2-4. CLI: --broker paper|analyze, --base-url, --api-key-env (default OPENALGO_API_KEY).
Tests: full signals->ledger run with a fake OpenAlgo transport (analyzer on / off / error / rejected).

### AZ3 - Owner run (not in sandbox)
- AZ3-1. Owner starts OpenAlgo locally, turns analyzer on, runs scripts/run_paper.py --broker analyze
  on a signals file.
- AZ3-2. Owner checks: orders visible in OpenAlgo sandbox order book, audit and ledger agree.
Sandbox cannot run OpenAlgo (httpx and openalgo are not installable here), so AZ3 is owner-only.

## 5. Not in scope

- Live orders, live feed, POV loop changes, risk limit changes, model or strategy changes.

## 6. Verification status (update each commit)

- AZ1: DONE in code and sandbox tests. `tests/unit/test_analyze_mode.py` (46 tests): guard refusals,
  broker FILLED / REJECTED / UNKNOWN rules, and a real local HTTP round trip (stdlib transport against
  a fake OpenAlgo server speaking the same JSON shapes).
- AZ2: DONE in code and sandbox tests. `scripts/run_paper.py --broker analyze` runs signals -> gate ->
  queue -> AnalyzeBroker -> ledger -> audit. Restart does not resend.
- Sandbox result (2026-11-06): `pytest tests/unit tests/parity` = 27 failed, 1714 passed. The 27 are
  the HRP tests (`test_phase0_hrp_cov.py`, `test_phase2_portfolio.py`), which also fail on the
  untouched commit. Not caused by this change.
- AZ3: NOT RUN. Needs owner machine with OpenAlgo running and analyzer on.

## 7. Assumptions to check in AZ3 (not verified against a live OpenAlgo here)

- orderstatus reply carries order_status and average_price, either at top level or under "data".
- placeorder success reply carries "orderid" at top level (matches vendored place_order_service).
- analyzer reply carries data.analyze_mode as a real boolean (matches vendored analyzer service).
- Sandbox order state reaches "complete" with a price. If it stays "open", fills show as UNKNOWN.
