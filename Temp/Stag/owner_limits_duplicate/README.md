# Parked: duplicate owner limits (moved 2026-11-25)

`owner_limits.py` (was src/quant/engines/core/owner_limits.py) duplicated the live owner caps.
The live gate reads `src/quant/core/owner_limits_store.py` through `src/quant/risk/gate.py`.
Nothing in live code imported this file. Owner decision 2026-11-25: retire it.

Also parked here: `test_owner_limits.py` (its tests) and `tests_removed_from_audit_2026_11_17.py`
(two tests of the duplicate removed from src/quant/tests/test_audit_2026_11_17_fixes.py).
