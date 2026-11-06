"""FastAPI dashboard. Reads the paper ledger, audit log and kill switch. Never sends orders.
Only write: POST /api/limits, which needs the DASHBOARD_API_TOKEN header (decision 0020).

Run (after installing requirements, on the owner's machine):
    python dashboard/app.py            # serves http://127.0.0.1:8050
Flow stage: Dashboard
Status: PHASE 3 IMPLEMENTED (paper data only; FastAPI imported at run time)
Data logic: src/quant/reporting/dashboard_data.py
"""
from __future__ import annotations

import hmac
import json
import os
import sys
from pathlib import Path
from typing import Dict, Optional

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from quant.audit.log import AuditLog  # noqa: E402
from quant.core.config import RiskLimits  # noqa: E402
from quant.core.env import load_env_file  # noqa: E402
from quant.core.limits_store import apply_limit_updates  # noqa: E402
from quant.monitor.ledger import Ledger  # noqa: E402
from quant.reporting.dashboard_data import build_overview  # noqa: E402

TOKEN_ENV = "DASHBOARD_API_TOKEN"
TOKEN_HEADER = "X-Dashboard-Token"

DEFAULT_PATHS = {
    "ledger": ROOT / "data" / "paper" / "ledger.jsonl",
    "audit": ROOT / "data" / "paper" / "audit.jsonl",
    "kill": ROOT / "data" / "paper" / "kill_switch.json",
    "limits": ROOT / "config" / "risk_limits.yaml",
    "settings": ROOT / "config" / "settings.yaml",
    "page": ROOT / "src" / "quant" / "index.html",
    "flow": ROOT / "dashboard" / "assets" / "flow_svg.js",
}

API_INDEX = {
    "service": "Algo-Pro dashboard",
    "page": "/ui",
    "endpoints": ["/api/overview", "/api/positions", "/api/events", "/api/health", "POST /api/limits (token)"],
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
    from fastapi import FastAPI, Request
    from fastapi.responses import FileResponse, JSONResponse

    load_env_file()  # reads the repo-root .env without overriding shell variables
    paths = {**DEFAULT_PATHS, **(paths or {})}
    app = FastAPI(title="Algo-Pro dashboard", version="0.1.0")

    @app.get("/")
    def index():
        return JSONResponse(API_INDEX)

    @app.get("/ui")
    def ui():
        """The one page: reads /api/overview every 2 s, writes limits through POST /api/limits."""
        return FileResponse(paths["page"], media_type="text/html")

    @app.get("/assets/flow_svg.js")
    def flow_asset():
        return FileResponse(paths["flow"], media_type="application/javascript")

    @app.post("/api/limits")
    async def set_limits(request: Request):
        """Save user-set risk limits. Needs the DASHBOARD_API_TOKEN header. Refused if no token is set."""
        expected = os.environ.get(TOKEN_ENV, "")
        if not expected:
            return JSONResponse({"error": f"limit writes disabled: {TOKEN_ENV} is not set"}, status_code=503)
        if not hmac.compare_digest(request.headers.get(TOKEN_HEADER, ""), expected):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        try:
            result = apply_limit_updates(paths["limits"], await request.json())
        except ValueError as exc:  # JSON errors are ValueError subclasses too
            return JSONResponse({"error": str(exc)}, status_code=422)
        AuditLog(paths["audit"]).append("limits_changed", {**result, "actor": "dashboard-api"})
        return JSONResponse({"changed": result})

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
