# 0014 — Vendor the OpenAlgo SDK (openalgo-python-library v2.0.3)

Date: 2026-11-05
Status: ACCEPTED (owner: "Add sdk").

## Context
Blue_engine (through src/quant/core/blue_bridge.py) imports the SDK package `openalgo` (`from openalgo import api`).
The platform copy (decision 0013) does not provide that package. The SDK is a separate project.

## Decision
- Vendor the SDK package folder `openalgo/` and its LICENSE (MIT) into `src/quant/vendor/openalgo/`, pinned to tag v2.0.3, commit 90f9316ef142497d607c2848bbe389e09987d8e3. Notice in NOTICE.md.
- Not copied: tests, docs, examples, Rust crate, benchmarks, playground, CLAUDE.md, and other SDK files.
- `blue_bridge.py` adds `src/quant/vendor` to the END of sys.path. A pip-installed SDK still takes priority.
- Added runtime deps the SDK needs to `requirements.txt`: `httpx>=0.27.0,<1.0`, `websocket-client>=1.8.0,<2.0`. These are third-party packages and are installed by pip, not copied.

## Licence
- SDK: MIT. Permissive. Keep LICENSE with the copy.
- Platform (decision 0013): AGPL-3.0. Separate folder, separate licence. The SDK does not pull AGPL code in.

## Sandbox limit
The cloud sandbox cannot install packages, so `httpx` and `websocket-client` are missing here. The SDK path resolves to the vendored copy, and the import fails only at `httpx`. Tests that need the SDK still fail here (same 70 as before). They must run on the owner machine after `pip install -r requirements.txt`.

## Rule exceptions
`tests/unit/test_layout.py` and `tests/unit/test_phase3.py` skip the `vendor/` folder, like `openalgo_platform/` (decision 0013). Our code stays fully checked.

## Consequence
Owner machine: run the unit suite after installing requirements. Expect the 70 `openalgo` tests to run, not skip.
