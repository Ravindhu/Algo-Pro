# LLM Per-Stage TTL and Call Reduction: Implementation Plan

Status: IMPLEMENTED (Phases 1-4 + Phase 5 instrumentation, 2026-11-12).
Phase 5 live comparison PENDING on this host: no LLM backend reachable
(no Ollama installed, no OPENROUTER_API_KEY). One command when a backend is up:
`py Temp/measure_llm_calls.py --cycles 4 --live-llm`.
Instrumentation added: `paper_shift.llm_calls()` + per-cycle `llm_tokens`
snapshot; offline plumbing pass verified (12 advised stage-runs / 4 cycles,
meter wired, 0 schema rejections).
Target path in repo: `docs/LLM_STAGE_TTL_PLAN.md`
Owner: Ravindhu. Date: 2026-11-12.

## 1. Goal

Cut LLM calls and token spend in the frontline agent, sector advisor and PM note.
Keep answer quality. All LLM output stays advice only and passes the schema
validator and safe-direction clamp.

## 2. Current state (verified in code)

| Site | Cache today | Problem |
|---|---|---|
| Frontline `advise()` | In-memory governor, global TTL 900 s | Cache key includes `session_id`, so no hits across runs. Non-tool path has no per-run call cap. |
| Sector advisor | None | Calls every run when enabled. Prompt changes every run. |
| PM note (`write_pm_note`) | Disk, no expiry | Key holds weights and vol, so it almost never hits. |
| Economic scorer (`llm_scorer`) | Disk, no expiry, keyed on text | Acceptable. |
| Views extraction (`extract_views`) | Disk, keyed on text | Acceptable. |

`LLMClient.for_named_site` turns the in-memory governor off for the sector
advisor, scorer and PM note.

## 3. Per-stage TTL (proposed)

Add under `engines.agent.cache` in `Blue_engine/input/macro_master.json`:

| Stage | TTL (s) | Reason |
|---|---|---|
| economic | 21600 | Macro data is daily to monthly |
| sector | 86400 | One advice per day after close |
| market | 900 | Microstructure changes faster |
| screener | 86400 | Daily screen |
| portfolio | 0 | No cache until a rebalance trigger exists |
| risk | 0 | Safety stage, always fresh |
| valuation, backtest, optimization | 86400 | Offline, not per cycle |

TTL 0 means always call. These numbers are proposals, to be checked on paper runs.

## 4. Phases

**Phase 0 (this doc).** Commit the plan. No runtime change.

**Phase 1: config.** Add the `stage_ttl_seconds` block. Default every value to 0,
so behaviour is unchanged until opted in.

**Phase 2: governor and frontline.**
- `core/llm/tokens.py`: accept a per-call TTL.
- `core/llm/router.py`: pass the TTL through `complete()`.
- `core/frontline_agent.py`: send each stage's TTL with its call.
- Remove `session_id` from the cache key so hits survive across runs.
- Persist entries to `data/llm_cache/stage/` so restarts keep them.

**Phase 3: sector advisor and PM note.** Hash rounded, trimmed inputs. Reuse
advice within its TTL. Cached advice still goes through validation and the clamp.

**Phase 4: tests** (in `Blue_engine/tests/`):
- Hit inside TTL. Miss after TTL.
- Risk and portfolio are never cached.
- A cached "no veto" is never returned for risk.
- Failed calls are never cached.
- A new session still gets the hit.
- Cached advice still passes validation.
- A second identical run makes zero LLM calls.

**Phase 5: measure.** Run `core/paper_shift.py` before and after. Compare calls
per cycle.

## 5. Acceptance

- Full `pytest` green (AGENTS.md step 12).
- Calls per cycle drop on paper_shift, with no new schema rejections.
- SOP section 16 updated. Work-log entry appended.
- No module in `risk/`, `execution/`, `portfolio/` or `monitor/ledger.py`
  imports `core/llm/*`.

## 6. Rollback

Set every `stage_ttl_seconds` value to 0. No code revert needed.

## 7. Risks

- Stale advice if TTLs are too long. Mitigation: per-stage TTLs, paper-tested.
- A cache hiding a changed input. Mitigation: the hash covers all inputs.
- Quality drift. Mitigation: compare advice on paper before enabling.
