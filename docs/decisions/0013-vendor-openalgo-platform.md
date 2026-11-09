# 0013 — Vendor the full OpenAlgo platform into src/quant (option 1)

Date: 2026-11-05
Status: ACCEPTED by owner ("Option 1 — OpenAlgo is open source, so you can use it").

## Context
The owner asked for a complete copy of the OpenAlgo platform inside `src/quant/`.
Upstream: https://github.com/marketcalls/openalgo, licence GNU AGPL-3.0 (License.md).
Copy: `src/quant/openalgo_platform/`, tracked files at upstream commit recorded in its NOTICE.md. No `.git` folder. No local edits.

## Licence consequence (AGPL-3.0)
- The vendored folder keeps its LICENSE (AGPL-3.0).
- AGPL-3.0 has network-use terms: a modified version run as a network service must offer its source. This repo is private, so no source offer is triggered today. Any future distribution or hosted use of a modified platform must follow AGPL-3.0.
- The owner accepted this. Owner to confirm the repo licence before any public release.

## Rule exceptions (recorded)
1. `tests/unit/test_layout.py` and `tests/unit/test_phase3.py::test_only_broker_adapter_and_scripts_import_openalgo_client` skip `openalgo_platform/` (import check, stage-marker check, order-call check, broker-adapter import check). Our own code is still fully checked.
2. The platform contains `.placeorder(` in its own files (routes, scripts, tests). The rule "only execution/openalgo_client.py may place orders" applies to OUR code. The platform is OpenAlgo itself. Our code must still not call the platform's order path directly; orders go through the risk gate and `openalgo_client.py`.
3. `pyproject.toml` excludes `quant.openalgo_platform*` from package discovery.

## Not solved by this copy
- Our code imports the SDK package `openalgo` (`from openalgo import api`, `from openalgo import ta`). The platform does not provide that package; it imports it too. So this copy does NOT make the 70 `openalgo` test failures pass.
- Next step needed: vendor or pip-install the SDK (`marketcalls/openalgo-python-library`, tag v2.0.3, git submodule at Temp/openalgo-python-library). Its licence must be checked first.

## Pin mismatch
`openalgo.UPSTREAM_PIN` (018a1dd4b9bc) does not match the copied commit. Reconcile before production use.

## Pin reconciled (2026-11-05)
- Old pin 018a1dd4b9bc was path-dependent: the roll-up hashed the absolute folder path, so it could never match another folder. Fixed in scripts/verify/upgrade_check.py (path reported, not hashed).
- New pin: content-only 9a075af0d435 = upstream commit e2797fd. Matches the vendored copy and the upstream clone.
- Owner to verify: run `python scripts/verify/upgrade_check.py --openalgo-dir <your openalgo folder>` on the running install. If it prints 9a075af0d435, the running code is the same content as the vendored copy. If not, the running install is a different version.
