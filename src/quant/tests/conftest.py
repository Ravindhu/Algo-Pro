"""Package-level test convention for the src/quant suite.

WHY THIS FILE EXISTS
--------------------
The quant tests live inside the package they test (`src/quant/tests/`), so they
are run straight from a checkout as well as from the installed package. Two
things have to be true for that to work, and neither is automatic:

1. `import quant...` must resolve.
   pytest's default `prepend` import mode puts the test file's rootdir on
   `sys.path`. For a file at `src/quant/tests/x.py` the rootdir is `src/quant`,
   so `quant` itself was NOT importable and every module that does
   `from quant.reporting... import ...` failed at collection with
   `ModuleNotFoundError: No module named 'quant'`. The repo venv has the package
   installed with `pip install -e .`, but the suite must not depend on that -
   a plain checkout has to work.

2. The repo root must be on `sys.path` too.
   `tests/` and `tests/fixtures/` live at the repo root and are read by path
   (the holiday CSV, the NSE event-calendar sample). The moved tests resolve
   them through `parents[3]`, which is the repo root; this file makes that
   explicit rather than implicit.

Both inserts are idempotent and ordered so the checkout wins over any
site-packages copy of `quant`.

KILL SWITCH
-----------
The Blue latching kill switch is isolated for every test by
``tests/conftest.py`` at the repo root, which applies to this folder too. Do not
duplicate that fixture here.
"""
from __future__ import annotations

import pathlib
import sys

# src/quant/tests/conftest.py -> parents[3] is the repo root (Algo-Pro/).
REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
SRC_DIR = REPO_ROOT / "src"

for entry in (str(REPO_ROOT), str(SRC_DIR)):
    if entry not in sys.path:
        sys.path.insert(0, entry)
