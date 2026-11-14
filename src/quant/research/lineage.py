"""Records data hash, code commit and parameters for each run, so results can be reproduced.

Flow stage: Experiment lineage
Status: PHASE 1 IMPLEMENTED
Existing code to reuse or wrap (do not copy): src/quant/blue_engine/core/ledger.py
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict


def record_lineage(out_path, data_path, code_commit: str, params: Dict[str, Any], strategy_id: str) -> Dict[str, Any]:
    data_sha = hashlib.sha256(Path(data_path).read_bytes()).hexdigest()
    params_sha = hashlib.sha256(json.dumps(params, sort_keys=True, default=str).encode()).hexdigest()
    row = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "strategy_id": strategy_id,
        "data_path": str(data_path),
        "data_sha256": data_sha,
        "code_commit": code_commit,
        "params_sha256": params_sha,
        "params": params,
    }
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, sort_keys=True, default=str) + "\n")
    return row
