# SEBI regulatory checklist — retail algo trading via broker API

Flow stage: none (compliance record)
Status: CHECKLIST PREPARED 2026-11-08. Every item needs an OWNER VERDICT. No item is marked compliant here.

## Sources

Verified against the circular itself, not a summary:

- [SEBI circular SEBI/HO/MIRSD/MIRSD-PoD/P/CIR/2025/0000013, 4-Feb-2025](https://www.sebi.gov.in/sebi_data/attachdocs/feb-2025/1738665456458.pdf) — the framework.
- [NSE circular INVG67858](https://nsearchives.nseindia.com/content/circulars/INVG67858.pdf) — "Implementation Standards for safer participation of retail investors in Algorithmic Trading", under clause 7 of the SEBI circular. (PDF text did not extract; read it directly before signing.)
- [MCX FAQ](https://www.mcxindia.com/faq/technology-services) — confirms static IP is required for the "tech-savvy investor using API" route, and documents the IBT/STWT tag prefix.
- Two secondary write-ups used only for orientation, not for any requirement here: [Zerodha Z-Connect explainer](https://zerodha.com/z-connect/business-updates/explaining-the-latest-sebi-algo-trading-regulations), [Groww overview](https://groww.in/blog/sebi-regulations-on-algorithmic-trading-in-india).

## Who this system is, regulatorily

Our route is the **"tech-savvy retail investor using a broker's client API"** — the self-developed algo case in section I(c) of the circular. We are NOT an algo provider or vendor: no strategy is shared with anyone, so no empanelment or vendor API key applies.

## The checklist

### A. Static IP whitelisting — REQUIRED for our route

Circular I(d) and NSE Annexure A: API access needs a whitelisted static IP; one primary and optionally one secondary for redundancy.

- [ ] Confirm the deployment machine has a static public IP (home broadband is usually dynamic — a business line or a cloud VM fixes this).
- [ ] Whitelist it with the broker.
- Owner verdict: ______________

**System impact:** none in code — the whitelisting happens at the broker. But `run_live.py` should refuse to start from a host whose egress IP is not the whitelisted one, once the owner confirms the address. OPEN ITEM: add an optional `allowed_egress_ip` to settings and check it at startup.

### B. OAuth + 2FA — broker-side, but verify what our broker gives us

Circular I(d): brokers must use OAuth-based authentication only, with two-factor authentication. All other mechanisms are to be discontinued.

- [ ] Confirm the broker's API is OAuth + 2FA (Ask the broker; this is their obligation, not ours to build).
- [ ] Our `.env` holds `OPENALGO_API_KEY`. Confirm the key is issued under the broker's new OAuth flow, not a legacy token.
- Owner verdict: ______________

### C. Order-per-second threshold → registration decision — THE KEY ITEM

Circular I(c): a self-developed algo must be **registered with the exchange through the broker only if it crosses the specified order-per-second threshold**. The threshold itself is set by the Broker's Industry Standards Forum under the exchanges.

- Our throttle: `execution/openalgo_client.py` caps orders at **8/second** (AnalyzeBroker.ORDER_RATE_PER_SEC — 80% of OpenAlgo's documented 10/s for placeorder).
- [ ] Get the exchange's published threshold from the broker. If it is above our realistic rate, we stay unregistered; if at or below, register the algo through the broker BEFORE live use.
- [ ] Record the threshold value and its source here once known: ______________
- Owner verdict: ______________  (below threshold / register the algo)

**System impact:** our per-second rate is already bounded in code. If registration becomes necessary, `intent.strategy_id` is what maps to the registered algo, and `execution/openalgo_client.py` already sends it as the `strategy` field on every order — so the plumbing for tagging exists either way.

### D. Order tagging / unique algo identifier — broker applies it; we must pass the right field

Circular II(b): all algo orders are tagged with a unique identifier from the exchange. Per the MCX FAQ, for client-direct API retail algos the tag's first 12 digits are `444444444444` and the 13th is `1`.

- In our stack the broker/OpenAlgo applies the tag; our job is to send a stable strategy identifier on every order, which we do (`strategy` field, plus `tag=intent_id` for the audit trail — both already in `openalgo_client.place()`).
- [ ] Confirm with the broker how the exchange tag maps to our `strategy` field, and whether OpenAlgo passes it through untouched.
- Owner verdict: ______________

### E. Immutable audit trail — ALREADY BUILT

Circular II(b) requires the audit trail. Our equivalent: `audit/log.py` is append-only and hash-chained (each record carries the previous record's hash; `verify()` detects any edit). Every signal, intent, gate decision and override is already written there.

- [x] Append-only, hash-chained audit log with verify() — implemented and tested.
- [ ] Owner confirms the retention location (data/live/audit.jsonl today) satisfies their record-keeping expectation.

### F. Kill switch — ALREADY BUILT AND DRILLED

The framework's risk-mitigation expectation (order throttle, kill switch) is met:

- [x] Kill switch: `risk/kill_switch.py`, persisted, survives restart, reset needs a named person. Drilled end-to-end with zero orders sent (Phase 3 exit check 4, `monitor/drill.py`, decision 0031).
- [x] Order throttle: `monitor/throttle.py` TokenBucket, 8/s enforced before any send.
- [x] Auto-kill on daily loss: `risk/auto_kill.py`, wired in the runner.

### G. Monthly mock / drill participation

The MCX FAQ references a mandatory monthly mock for tech-savvy API clients.

- [ ] Confirm with the broker whether this applies, and diary it.
- Owner verdict: ______________

## Honest limits of this checklist

1. I have verified the REQUIREMENTS from the circular text, but the exchange **threshold number** and the broker's **OAuth/tag mapping** can only come from the broker and the exchange circulars. Those two facts gate items C and D.
2. This document is not legal advice. The owner signs the verdicts; if any answer is unclear, confirm with the broker's compliance desk before arming live mode.
3. Nothing here was tested against a live broker. "Verified" above means verified in code/tests, which is a different claim.
