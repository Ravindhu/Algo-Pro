# Implementation plan: one script per OS (2026-11-07)

Plain English. Nothing here sends an order.

## Goal
- Windows: `install.bat` does install, update and start.
- Ubuntu: `Servers/launch_ubuntu_server.sh` does clone, install, update and start.
- Unwanted duplicates move to `Temp/archive/2026-11-07/installers/` (git-ignored; git history keeps every copy).

## Decisions taken (owner, 2026-11-07)
- One script per OS. Daily start = the same script (double-click on Windows).
- Retired: `install.sh`, `scripts/ops/START_ALL.cmd`, `scripts/ops/START_OPENALGO.bat`.
- Kept as shims: `scripts/start_openalgo.py`, `scripts/upgrade_openalgo.ps1` (scheduled tasks may call them).
- Updates reuse existing safe logic. Windows: `scripts/ops/upgrade_openalgo.ps1`. Ubuntu: an update function in the launcher with the same gates.

## Status
| Phase | Scope | Status |
|---|---|---|
| 0 | Audit and plan | Done |
| 1 | Windows `install.bat`: repo pull, install, safe OpenAlgo upgrade, start | Done in code. Not run on Windows here |
| 2 | Ubuntu `launch_ubuntu_server.sh`: clone, pass `--domain/--email`, gated update | Done in code. `bash -n` passes. Not run on a server here |
| 3 | Docs: `INSTALL.md`, SOP section 22, `DEPLOY_UBUNTU.md`, `README.md`, `AGENTS.md`, `scripts/README.md` | Done |
| 4 | Owner test on a clean Windows PC and a test VPS | Pending owner |

## Tests run here
- `tests/unit/test_layout.py`: 4 passed.
- Full `tests/unit`: 27 failed, 816 passed. The 27 are the known HRP baseline, unchanged.
- No test covers the installers.

## Safety rules kept
- Engine stays stopped at boot. `pov-runner` is never enabled by the installer and never restarted by the update.
- TLS stays the default. Plain HTTP needs `--allow-plain-http`.
- `.env` and `db/` still move together.
- No installer step places an order.
- Updates refuse in NSE hours (09:15-15:30 IST) and while `pov-runner` runs.

## Known gaps
- Exchange holidays are not checked by the Ubuntu update gate.
- Windows update relies on `upgrade_openalgo.ps1`, which hard-codes `C:\Program Files\Git\cmd\git.exe`.
- The live VPS must not be updated until Phase 4 passes.

## Next priority
Owner tests Phase 4 on a clean Windows PC, then a test VPS.
