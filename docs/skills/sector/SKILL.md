# `frontline.sector` — Sector allocation skill

**Stage:** `sector` · **Engine:** `sectors/sector_engine.py` ·
**Publishes:** `data/agent_stage/sector.json`

## Purpose

Turn sector relative-strength matrices plus qualitative context into
`INVEST_MORE` / `WAIT_AND_SEE` stances. This is the only stage with its own
prompt (`SECTOR_AGENT_PROMPT`) and its own validator
(`sectors/sector_engine.py::_validate_advice`), so its skill contract is
declared here for the dashboard and audit only — the engine validates.

## Output shape

| Key | Meaning |
|---|---|
| `allocations` | per-sector stance map, validated by `_validate_advice(..., sector_names, sector_scores)` |
| `rationale` | max 40 words |

## Evidence it wants

None mandatory. With no matrix, the engine must not invent a stance.

## Hard safety rule (already implemented, do not weaken)

Any invalid, unavailable or failed advice is converted to **zero-allocation
`WAIT_AND_SEE` limits**. The failure mode of this skill is *no capital
deployed*, never an aggressive stance on missing evidence. See
`tests/test_sector_engine.py`.

## What it may **not** do

- Invent a sector name that is not in the supplied `sector_names` (validator
  rejects).
- Write anything but `sector.json`.
- Place, size or cancel an order.

## Enabling

`engines.sector.agent_enabled` (default `false`) and
`engines.sector.pipeline_enabled` (default `false`). Paper-run first — a change
here changes the traded universe.

## Tests

`tests/test_sector_engine.py`, `tests/test_sector_ranker_eval.py`
