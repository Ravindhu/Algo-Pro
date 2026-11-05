# 0012 — Keep Blue-only features inside src/quant (D2)

Date: 2026-11-05
Status: ACCEPTED (owner choice D2: "keep inside the quant")

## Context
Blue_engine has features the quant system does not have yet: the optimizer, screener, economics, agent pipeline, and POV engine.
Decision 0011 removes Blue_engine after the quant code no longer loads it. That would lose these features unless they move.

## Decision
Keep these features inside `src/quant/` (moved and re-structured to the AGENTS.md layout, not left in Blue_engine):
- optimizer  -> src/quant/research/ (or feedback/ for re-optimisation)
- screener   -> src/quant/data/universe.py and a screener module
- economics  -> src/quant/data/market_inputs.py (macro, flow inputs) and models/
- agent pipeline -> src/quant/feedback/ and monitor/ (advice only; never on the trade path, AGENTS rule 4)
- POV engine -> src/quant/execution/pov.py (sizing and schedule only; execution stays deterministic)

The OpenAlgo SDK is NOT copied into the repo. It stays a pinned pip dependency (see section "OpenAlgo" below).

## Consequences
- Stage 6 is added to docs/DECOUPLING_PLAN.md: migrate these features. Not started.
- Each migrated feature needs its own tests and a Flow stage line that matches the SVG.
- Some Blue tests (176 collected) move with their features.
- AGENTS.md "wrap, never copy" is overridden for the migrated code. Recorded here.

## OpenAlgo
- The SDK (`openalgo` Python package) is a dependency. It must be installed (pip, pinned to the SDK version in Temp/openalgo-python-library, tag v2.0.3). It is not copied into src/quant.
- The OpenAlgo platform (the server app) is a separate project. It is cloned on the VPS by the installer. It is never copied into this repo.
- Moving the SDK code into src/quant would not be enough: the platform is a separate app, and copying it would break the rule in AGENTS.md that OpenAlgo stays modular and upgradeable.
