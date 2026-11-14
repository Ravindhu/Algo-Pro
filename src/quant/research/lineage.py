"""Experiment lineage: everything needed to reproduce a run, recorded once.

Flow stage: Experiment lineage
Status: PHASE 1 IMPLEMENTED; full record 2026-11-14
Existing code to reuse or wrap (do not copy):
    src/quant/blue_engine/core/ledger.py (the JSONL append pattern this mirrors;
    the engine has no experiment-lineage store, so the record shape is defined here).

WHAT A LINEAGE ROW MUST ANSWER
------------------------------
A result you cannot reproduce is a rumour. To rebuild a run you need FOUR things,
and the original record had only two:

  1. the DATA          - content hash  (had it)
  2. the PARAMETERS    - content hash  (had it)
  3. the CODE          - commit          (had it, but not whether the tree was dirty)
  4. the ENVIRONMENT   - cost model version, horizon, universe, date range (MISSING)

Point 4 is what actually bites. A backtest at 10 bps costs and one at 25 bps are
different experiments that produce identical parameter hashes, so a trial log
alone cannot tell them apart. ``record_lineage`` now captures the run
environment and ``verify_lineage`` can prove a stored row still matches reality.

DESIGN RULES
------------
- Append-only JSONL. A lineage row is never edited; a correction is a new row.
- Every hash is over bytes, so whitespace and key order cannot hide a change.
- A dirty working tree is recorded explicitly: "ran on commit X plus uncommitted
  changes" is a real, reproducible-enough state, but only if it is written down.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

__all__ = [
    "record_lineage",
    "read_lineage",
    "verify_lineage",
    "code_state",
    "sha256_file",
    "sha256_params",
]

#: Schema version of the lineage row. Bump when a field changes meaning.
SCHEMA_VERSION = 2


def sha256_file(path) -> str:
    """Content hash of a file. Missing file raises: a hash of nothing is a lie."""
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def sha256_params(params: Dict[str, Any]) -> str:
    """Stable hash of a parameter dict, independent of key order."""
    return hashlib.sha256(
        json.dumps(params, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def code_state(repo_root=None) -> Dict[str, Any]:
    """Current commit and whether the tree is dirty. Never raises.

    A dirty tree is recorded as such rather than refused: refusing would push
    people to bypass the record entirely, which is worse than an honest "dirty".
    """
    root = Path(repo_root) if repo_root else Path(__file__).resolve().parents[3]
    out: Dict[str, Any] = {"commit": "unknown", "dirty": None, "available": False}
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=str(root),
            capture_output=True, text=True, timeout=10)
        if commit.returncode == 0:
            out["commit"] = commit.stdout.strip()
            out["available"] = True
        status = subprocess.run(
            ["git", "status", "--porcelain"], cwd=str(root),
            capture_output=True, text=True, timeout=10)
        if status.returncode == 0:
            out["dirty"] = bool(status.stdout.strip())
    except Exception:  # noqa: BLE001 - lineage must not break a run
        pass
    return out


def record_lineage(
    out_path,
    data_path,
    code_commit: str,
    params: Dict[str, Any],
    strategy_id: str,
    *,
    dataset: Optional[str] = None,
    date_range: Optional[tuple] = None,
    horizon: Optional[int] = None,
    cost_model_version: Optional[str] = None,
    n_trials: Optional[int] = None,
    universe: Optional[str] = None,
    extra: Optional[Dict[str, Any]] = None,
    repo_root=None,
) -> Dict[str, Any]:
    """Append one lineage row. Returns the row as written.

    The first five arguments keep the original signature, so an existing caller
    keeps working. The keyword arguments are the environment record that makes a
    run reproducible.

    ``code_commit`` is taken as given, but ``git_state`` is probed independently
    and a mismatch is recorded - a caller cannot accidentally claim a clean tree.
    """
    row: Dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "ts": datetime.now(timezone.utc).isoformat(),
        "strategy_id": strategy_id,
        "data_path": str(data_path),
        "data_sha256": sha256_file(data_path),
        "code_commit": code_commit,
        "params_sha256": sha256_params(params),
        "params": params,
        "dataset": dataset,
        "date_range": list(date_range) if date_range else None,
        "horizon": horizon,
        "cost_model_version": cost_model_version,
        "n_trials": n_trials,
        "universe": universe,
        "git_state": code_state(repo_root),
    }
    if row["git_state"]["available"] and row["git_state"]["commit"] != code_commit:
        row["git_state"]["commit_mismatch"] = True
    if extra:
        row["extra"] = extra

    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, sort_keys=True, default=str) + "\n")
    return row


def read_lineage(path) -> List[Dict[str, Any]]:
    """Every lineage row, oldest first. Missing file gives an empty list."""
    p = Path(path)
    if not p.exists():
        return []
    out: List[Dict[str, Any]] = []
    with p.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue  # a corrupt line must not hide the good ones
    return out


def verify_lineage(row: Dict[str, Any], data_path=None,
                   params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Check a stored row still matches the data and parameters it claims.

    Returns ``{"ok": bool, "problems": [...], "checked": [...]}.`` Nothing is
    fixed here: the point is to expose a mismatch, not to paper over it.
    """
    problems: List[str] = []
    checked: List[str] = []

    if data_path is not None:
        checked.append("data_sha256")
        try:
            if sha256_file(data_path) != row.get("data_sha256"):
                problems.append("data content changed since the run was recorded")
        except FileNotFoundError:
            problems.append(f"recorded data file is missing: {data_path}")

    if params is not None:
        checked.append("params_sha256")
        if sha256_params(params) != row.get("params_sha256"):
            problems.append("parameters differ from the recorded run")

    gs = row.get("git_state") or {}
    if gs.get("dirty"):
        checked.append("git_state")
        problems.append("run was recorded on a DIRTY tree; reproduction is approximate")

    return {"ok": not problems, "problems": problems, "checked": checked}
