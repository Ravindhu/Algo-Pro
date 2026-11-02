"""Where runtime state (sqlite stores, JSON state, caches) is written.

Every default location is ``<repo>/data/...``. Tests and CI must never write
into the real working tree, so one override redirects all of it:

    BLUE_RUNTIME_DATA_ROOT=/some/temp/dir

Stdlib only, and imported by modules at the top of the package, so importing
it cannot create a circular import.
"""
from __future__ import annotations

import os
from pathlib import Path

__all__ = ["runtime_data_root"]


def runtime_data_root() -> Path:
    """Return the root for runtime state. Honours BLUE_RUNTIME_DATA_ROOT."""
    override = os.getenv("BLUE_RUNTIME_DATA_ROOT")
    if override:
        return Path(override)
    # Blue_engine/runtime_paths.py -> repo root is one level above Blue_engine.
    return Path(__file__).resolve().parents[1] / "data"
