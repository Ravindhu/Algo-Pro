# Frontline Agent JSON-Patch Edit Protocol: Implementation Plan

Status: IMPLEMENTED (Phases A-D, 2026-11-12). Phase E (paper measurement) pending.
Owner: Ravindhu. Companion doc: `LLM_STAGE_TTL_PLAN.md` (per-stage TTLs).

## 1. Goal

Cut frontline-agent token spend and latency by asking the model to EDIT a
small JSON patch object instead of regenerating a full advice payload each
cycle. Advice stays advisory-only: same `_SCHEMAS` validation, same
`_escalate` safe-direction clamp, same fail-closed behaviour.

## 2. The eight required elements

| # | Element | Where it lands |
|---|---|---|
| 1 | JSON patch format | New `AdvicePatch` contract in `core/llm/patch_protocol.py`: model receives the LAST VALID advice plus a patch op list (`set`, `inc`, bounded by `_CAPS`) |
| 2 | Apply patch locally | `apply_patch(base, ops)` in the same module - pure Python, no model in the loop |
| 3 | Extract the target branch | `extract_target(engine, patch)` narrows the patch to the stage's own keys; foreign keys are dropped, not merged |
| 4 | Strip whitespace | `_strip_ws` normalises strings/keys before hashing and validating, so a cosmetic change never invalidates a cache hit |
| 5 | Define target functions | `_TARGETS: Dict[str, TargetFn]` - one pure function per engine stage extracting the comparable sub-object |
| 6 | Enforce strict schema | Patch output re-validated through the EXISTING `_validate(engine, ...)`; any unknown op/key rejects the whole patch (fail closed) |
| 7 | Switch low model for edit JSON | Edit-mode calls route to the cheapest available backend first (Ollama `qwen3:8b`, then OpenRouter `:free`) via `edit_model` config key |
| 8 | Cache the static schema | The per-engine schema instruction is compiled once per engine and memoised (`_instruction_cache`), never re-serialised per call; combined with the stage TTL cache this maximises provider prompt-cache hits |

## 3. Phases

- **Phase A (this doc).** Plan committed.
- **Phase B.** `core/llm/patch_protocol.py`: op set (`set`, `inc`), strict
  validation, local apply, target extraction, whitespace stripping, memoised
  schema compile helper.
- **Phase C.** `FrontlineAgent.advise(engine, ..., patch_mode=True)`: build the
  edit prompt (last advice + schema diff instructions), call with the low
  model, apply the patch locally, validate the RESULT through `_validate` +
  `_escalate` exactly like a fresh answer.
- **Phase D.** Tests: patch apply purity, strict rejection of unknown
  ops/keys, whitespace invariance, target-function narrowing, low-model
  routing, schema-cache single-compile, clamp still applied after patch.
- **Phase E.** Measure calls/tokens before vs after on paper_shift; SOP 16
  update; work log. STATUS: instrumentation done (`paper_shift.llm_calls`,
  per-cycle `llm_tokens`); live comparison blocked on this host - no Ollama,
  no OPENROUTER_API_KEY. Run `py Temp/measure_llm_calls.py --cycles 4
  --live-llm` when a backend is reachable. Offline plumbing pass: 12 advised
  stage-runs over 4 cycles, meter wired, 0 schema rejections, patch answers 0
  (expected offline).

## 4. Safety

- The patch path cannot loosen a veto: `_CAPS` escalation runs on the patched
  result, unchanged.
- Patch mode is opt-in per config (`agent.patch.enabled`, default false).
- Any malformed patch falls back to the existing full-regeneration call.
- Rollback: set `agent.patch.enabled=false`.

## 5. Risks

- Patch drift from the base advice. Mitigation: the base is the last
  validated advice and the result is re-validated wholesale.
- Cheap model producing worse edits. Mitigation: Phase E comparison run.
