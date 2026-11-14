"""Load the repo-root .env into os.environ. No Blue_engine import.

Flow stage: OpenAlgo (secret loading for the order client)
Status: PHASE 1 IMPLEMENTED
Existing code to reuse or wrap (do not copy): src/quant/blue_engine/core/client.py load_env_file (ported; same rules).
Decoupling stage 4, item 13 (docs/DECOUPLING_PLAN.md).

Rules (same as Blue's loader):
- The file is <repo root>/.env unless a path is given. It is git-ignored.
- Variables already set in the process win. A shell export is never replaced by the file.
- A missing file returns False and changes nothing. It never raises.
- Parsing uses python-dotenv (a listed project dependency in requirements.txt).
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

_log = logging.getLogger("quant.core.env")

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_ENV_FILE = REPO_ROOT / ".env"


def load_env_file(path: Optional[Path] = None) -> bool:
    """Load the .env file into os.environ without override. Return True if a file was parsed."""
    target = Path(path) if path is not None else DEFAULT_ENV_FILE
    if not target.is_file():
        return False
    try:
        from dotenv import load_dotenv
    except ImportError:  # pragma: no cover - python-dotenv is a project dependency
        _log.warning("python-dotenv is not installed; %s was not loaded", target)
        return False
    try:
        return bool(load_dotenv(target, override=False))
    except Exception as exc:  # noqa: BLE001 - loading secrets must never crash startup
        _log.warning("could not load %s: %s", target, exc)
        return False
