# Implementation plan: sandbox circuit band and screened-folder templates (2026-11-27)

Flow stage: Liquidity screener -> screen file (input enrichment); ops setup
Status: DONE (tests green)

## Problem 1 - every symbol dropped in analyze (sandbox) mode
OpenAlgo's sandbox quote reply carries no `upper_circuit_limit` / `lower_circuit_limit`, so
`band_from_quote` returns "quote carries no circuit limits" and `screen_rows` drops every name.
The live path must keep failing closed: a real broker band is required for live.

Decision: when OpenAlgo's analyzer (sandbox) is CONFIRMED ON for the refresh, a missing band
uses the NSE maximum band (MAX_CIRCUIT = 20%) and the row is labelled `band_source = sandbox_default`.
Orders in the sandbox are simulated, so this cannot reach the broker. If the analyzer state is
unknown or off, nothing changes. An asymmetric or implausible band is still rejected.

Not changed: the ASM/GSM rule. An unpublished ASM flag still excludes the name. If the sandbox quote
also lacks ASM, the chain will still drop names; that needs a separate decision.

## Problem 2 - easy install of data/screened/
`backup_state.py --setup` copies only top-level template files into `data/`. Add
`scripts/ops/templates/data/screened/` (README + the two switch/list files with safe defaults)
and create `data/screened/` from it. Existing files are never overwritten.

## Steps
1. [ ] `readonly_client.analyzer_mode(api_key)` -> True/False/None (None when unclear).
2. [ ] `circuit_band.enrich_candidates(..., sandbox_circuit_pct=None)`: fallback only for the
       "no circuit limits" case; sets band_source.
3. [ ] `screen_refresh`: pass the sandbox band through refresh_once; refresh asks the analyzer
       each cycle (None/exception -> no fallback).
4. [ ] Templates under `scripts/ops/templates/data/screened/`; `setup()` creates the folder.
5. [ ] Tests: fallback only with sandbox confirmed; live/unknown still drops; asymmetric still
       rejected; end-to-end chain passes with ASM published; setup creates and does not overwrite.
6. [ ] Full suite, secret scan, commit, push branch, fast-forward master.
