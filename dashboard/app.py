"""FastAPI dashboard. Read-only: it reads the paper ledger, audit log and kill switch, and never sends orders.

Run (after installing requirements, on the owner's machine):
    python dashboard/app.py            # serves http://127.0.0.1:8050
Flow stage: Dashboard
Status: PHASE 3 IMPLEMENTED (paper data only; FastAPI imported at run time)
Data logic: src/quant/reporting/dashboard_data.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Dict, Optional

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from quant.audit.log import AuditLog  # noqa: E402
from quant.core.config import RiskLimits  # noqa: E402
from quant.monitor.ledger import Ledger  # noqa: E402
from quant.reporting.dashboard_data import build_overview  # noqa: E402

DEFAULT_PATHS = {
    "ledger": ROOT / "data" / "paper" / "ledger.jsonl",
    "audit": ROOT / "data" / "paper" / "audit.jsonl",
    "kill": ROOT / "data" / "paper" / "kill_switch.json",
    "limits": ROOT / "config" / "risk_limits.yaml",
    "settings": ROOT / "config" / "settings.yaml",
}

API_INDEX = {
    "service": "Algo-Pro dashboard",
    "mode": "API only (no HTML page)",
    "endpoints": ["/api/overview", "/api/positions", "/api/events", "/api/health"],
}


def read_settings(path: Path) -> Dict[str, object]:
    """Dashboard section of settings.yaml. Missing file or key gives the defaults."""
    import yaml

    if not path.exists():
        return {"pnl_visible_default": True}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return {"pnl_visible_default": True, **(data.get("dashboard") or {})}


def load_state(paths: Dict[str, Path], last_prices: Optional[Dict[str, float]] = None) -> Dict[str, object]:
    """Read every source once and build the overview. Missing files mean an empty, safe state."""
    ledger = Ledger(paths["ledger"])
    audit = AuditLog(paths["audit"])
    kill = json.loads(paths["kill"].read_text(encoding="utf-8")) if paths["kill"].exists() else {}
    limits = RiskLimits.from_yaml(paths["limits"]) if paths["limits"].exists() else RiskLimits()
    fills = [e for e in ledger.events() if e.get("event") == "fill"]
    overview = build_overview(
        fills=fills,
        audit_records=audit.read_all(),
        kill=kill,
        limits={k: getattr(limits, k) for k in limits.__dataclass_fields__},
        last_prices=last_prices or {},
    )
    overview["settings"] = read_settings(paths["settings"])
    overview["feed"] = {"status": "not connected", "note": "No live feed exists yet (paper data only)."}
    return overview


def create_app(paths: Optional[Dict[str, Path]] = None):
    """Build the FastAPI app. Import happens here so the module loads without FastAPI installed."""
    from fastapi import FastAPI
    from fastapi.responses import JSONResponse

    paths = {**DEFAULT_PATHS, **(paths or {})}
    app = FastAPI(title="Algo-Pro dashboard", version="0.1.0")

    @app.get("/")
    def index():
        return JSONResponse(API_INDEX)

    @app.get("/api/overview")
    def overview():
        return JSONResponse(load_state(paths))

    @app.get("/api/positions")
    def positions():
        return JSONResponse(load_state(paths)["positions"])

    @app.get("/api/events")
    def events():
        return JSONResponse(load_state(paths)["events"])

    @app.get("/api/health")
    def health():
        state = load_state(paths)
        return JSONResponse({"feed": state["feed"], "mode": state["mode"],
                             "kill_switch": state["kill_switch"]["active"]})

    return app


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(create_app(), host="127.0.0.1", port=8050)
