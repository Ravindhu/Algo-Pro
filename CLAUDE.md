# Project Engineering & Agent Governance (`CLAUDE.md`)

> **To run the algo, read [docs/Agent/runalgo.md](docs/Agent/runalgo.md) first.** It has the start, stop and safety steps.

All development, refactoring, UI design, and testing in this repository must strictly follow the imported specification files below.

## Core Execution & Context Efficiency
@docs/TOKEN_OPTIMIZATION_RULES.md
@docs/ROADMAP_RULES.md

## System Architecture, Data & Backend
@docs/ARCHITECTURE_RULES.md
@docs/DATA_MODEL_RULES.md
@docs/DATABASE_RULES.md
@docs/API_RULES.md
@docs/SOURCES_RULES.md
@doc/QUANT_RULES.MD

## Observability, Verification & Terminal UI
@docs/LOG_RULES.md
@docs/TESTING_RULES.md
@docs/UI_RULES.md

## Mandatory Session Execution Protocol
1. **Plan First:** Before modifying code, inspect only the relevant files using targeted search/line ranges and verify alignment with `docs/ARCHITECTURE_RULES.md` and `docs/ROADMAP_RULES.md`.
2. **Apply Minimal Diffs:** Edit existing files surgically without rewriting unrelated sections.
3. **Verify Before Finishing:** Run affected tests (`docs/TESTING_RULES.md`) and ensure zero secret leaks (`docs/LOG_RULES.md`) before reporting completion.
