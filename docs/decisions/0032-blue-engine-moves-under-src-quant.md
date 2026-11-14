# 0032 — `Blue_engine/` moves to `src/quant/blue_engine/`

**Date:** 2026-11-14
**Status:** Accepted, implemented
**Owner instruction:** "move blueengine under quant . make it an single structured repo"

## Decision

The engine tree moves from the repo root to `src/quant/blue_engine/`. The
package name stays `blue_engine`; only the folder location changes.

```
before:  <repo>/Blue_engine/{core,alpha,pov,portfolio,ml,optimizer,...}
after:   <repo>/src/quant/blue_engine/{core,alpha,pov,portfolio,ml,optimizer,...}
```

## Why

1. **One tree.** Before this, the repo had two parallel Python source roots:
   `src/quant/` (the v5 flow) and `Blue_engine/` (the engine it wraps). Every
   tool, guard and test had to know about both.
2. **Fewer path special-cases.** `src/quant/core/blue_bridge.py` existed partly
   to bridge the folder-name/package-name mismatch (`Blue_engine/` vs
   `blue_engine`). Nesting the engine removes the mismatch from the layout.
3. **Packaging.** One `src` root means the root `pyproject.toml` `packages.find`
   can see the engine tree, instead of requiring a separate editable install
   from a second `pyproject.toml`.

## What changed

| Area | Change |
|---|---|
| Files | 389 tracked files moved with `git mv`, so history follows each one |
| `src/quant/core/blue_bridge.py` | `_SOURCE_DIR` is now `parents[1] / "blue_engine"` |
| Root `pyproject.toml` | `testpaths` entry now `src/quant/blue_engine/tests` |
| Engine `pyproject.toml` | `package-dir` map is unchanged (all paths are relative to the file, which moved with the tree); `pythonpath` corrected from `["."]` to `[".."]` |
| `tests/conftest.py`, engine `tests/conftest.py` | docstring paths; package root is still `parents[1]` because the tests folder moved with the tree |
| `tests/parity/*` | `parents[2] / "Blue_engine"` → `parents[2] / "src" / "quant" / "blue_engine"` |
| `tests/unit/test_watchlist.py`, `test_stage_skills.py`, `test_screener_bridge_launcher.py` | relative-depth paths corrected |
| `scripts/**` | 14 `sys.path.insert` lines repointed |
| `install.bat`, `Servers/install_ubuntu.sh`, `Servers/launch_ubuntu_server.sh` | editable-install path |
| `.gitignore` | engine ignore/negate rules |
| `tests/unit/test_case_paths.py` | guard re-based: it now blocks a dead `Blue_engine/` literal and a nested wrapper folder |
| `src/quant/tests/test_layout.py` | `blue_engine` added to `VENDOR_DIRS` (see below) |
| `AGENTS.md`, `docs/quant_system_flow_v5.md` | path table and reuse map |

## Two guards needed a real decision, not a path edit

1. **`src/quant/tests/test_layout.py`** asserts that every module under
   `src/quant/` imports, carries a `Status:` line, and never names
   `.placeorder(`. The engine is neither a v5 stage nor our order path — it is
   the pre-existing code the stages wrap, and it reaches the SDK through its own
   client. Scanning it makes all three guards meaningless. `blue_engine` is
   therefore excluded from that scan exactly like the vendored OpenAlgo trees
   (decision 0013/0014). The guards still cover every v5 stage module.

2. **`tests/unit/test_case_paths.py`** scanned `src/` with `rglob`. `src/quant/
   openalgo_platform/.venv` and its `frontend/node_modules` are inside `src/`,
   so the scan walked a full virtualenv and hung instead of failing. It now
   prunes install trees. Runtime: over 3 minutes (hang) → 1.8 s.

## Two latent bugs found by the move

Both were dead relative literals that only worked when the process was started
from the repo root. They would have failed from anywhere else.

| File | Was | Now |
|---|---|---|
| `blue_engine/core/runner.py` | `DEFAULT_CONFIG = "Blue_engine/input/master.json"` | resolved from `__file__` |
| `blue_engine/optimizer/data_cache.py` | `_P("Blue_engine/input/strategy.active.json")` | resolved from `__file__` |

`IMPLEMENTATION_PLAN.md` X-29 had already flagged the `runner.py` one.

## Verification

Baseline = commit `4e05879` in a separate worktree, same interpreter, same options.

| Suite | Baseline | After move |
|---|---|---|
| `src/quant/blue_engine/tests` | 50 failed, 1786 passed, 6 skipped | see work log for the final count; 10 `test_stage_skills` failures introduced by depth were fixed |
| `tests/parity` | — | 1468 passed, 0 failed |
| `tests/unit` | 3 failed | the same 3 failed |
| `src/quant/tests` | 3 failed | 0 failed (layout guards fixed) |

Every remaining failure was confirmed present at the baseline commit. The known
causes are environment gaps (`pyarrow`/`fastparquet` absent: 39 tests;
`yfinance` absent) and pre-existing logic failures tracked in
`IMPLEMENTATION_PLAN.md` (X-28).

## Not changed

- The package name: still `blue_engine`. Imports did not change.
- `Blue_engine/input/*.json` file names: only their parent folder moved.
- Historical audit and status documents under `docs/audits/` and `docs/STATUS_*`:
  they record what was true on their date and were left as written.

## Consequences

- One source root. `src` is now the only place Python code lives.
- `git log --follow` still traces any engine file across the move.
- The bash installers now install `src/quant/blue_engine[charts]`.
- A future move of this tree must update the four depth-relative sites named
  above; `tests/unit/test_case_paths.py` fails loudly if one is missed.
