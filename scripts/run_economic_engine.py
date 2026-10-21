"""run_economic_engine.py - compatibility shim (scripts/ was grouped 2026-10-21).

The implementation now lives in ``scripts/run/run_economic_engine.py``. This file stays so
every documented command keeps working unchanged (SOP, README, deploy docs,
scheduled tasks). It imports the real module and lets it run as ``__main__``:
``sys.argv`` is inherited untouched, so flags behave exactly as before.
"""
import runpy
import sys
from pathlib import Path

_TARGET = Path(__file__).resolve().parent / "run" / "run_economic_engine.py"

if __name__ == "__main__":
    sys.argv[0] = str(_TARGET)
    runpy.run_path(str(_TARGET), run_name="__main__")
else:                                   # `import smoke_test` style callers
    import importlib.util

    _spec = importlib.util.spec_from_file_location("run_economic_engine_impl", _TARGET)
    _mod = importlib.util.module_from_spec(_spec)
    sys.modules["run_economic_engine_impl"] = _mod
    _spec.loader.exec_module(_mod)
    globals().update({k: v for k, v in vars(_mod).items()
                      if not k.startswith("__")})
