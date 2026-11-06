# Decision 0019: local .env loader replaces Blue's (decoupling stage 4, item 13)

Status: IMPLEMENTED 2026-11-06. Owner sign-off: PENDING (same review as decision 0018).

## What changed

- New `src/quant/core/env.py`: `load_env_file(path=None) -> bool`.
  - Reads `<repo root>/.env` by default. The file is git-ignored.
  - Uses `python-dotenv` (already in `requirements.txt`). Same parser as Blue's loader.
  - Never overrides a variable already set in the process.
  - A missing file returns False and changes nothing. It never raises.
- `src/quant/execution/openalgo_client.py`: `api_key_from_env()` calls the local loader.
  It no longer calls `blue_bridge.load("core.client")`.
- Old Blue-dependent test `tests/unit/test_phase0_client.py` moved to `Temp/Staging Workspace/`.
  Replaced by `tests/unit/test_dotenv_loading.py` (11 cases, no Blue needed).

## Why this is safe

- The key path still reads the environment only. Blue's `resolve_api_key()` (which can decrypt a key
  from the OpenAlgo database) is still never called.
- A missing key still fails closed with `RuntimeError`.
- An AST test fails if `core/env.py` or `openalgo_client.py` imports anything with "blue" in its name.

## Still open

- Stage 5 (remove Blue from the quant path) still needs stage 6 (feature migration) first.
- Owner sign-off on this change (see decision 0018).
