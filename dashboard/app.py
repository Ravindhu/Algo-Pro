"""FastAPI dashboard. Reads the paper ledger, audit log and kill switch. Never sends orders.

Writes (all need the DASHBOARD_API_TOKEN header, decision 0020):
    POST /api/limits       save user-set risk limits
    POST /api/kill/trip    block new entries (reason required, audited)
    POST /api/kill/reset   clear the kill switch (reason required, audited)
Reads: GET /api/overview, /api/positions, /api/events, /api/health, /api/kill.
Portfolio header: GET /api/portfolio reads broker holdings through OpenAlgo (read-only, 30 s cache, decision 0032).

Run (after installing requirements, on the owner's machine):
    python dashboard/app.py            # serves http://127.0.0.1:8050
Flow stage: Dashboard
Status: PHASE 3 IMPLEMENTED (paper data only; FastAPI imported at run time)
Data logic: src/quant/reporting/dashboard_data.py
"""
# No `from __future__ import annotations` here: FastAPI reads route annotations
# from module globals, and `Request` is a local name inside create_app.
import hmac
import json
import os
import sys
import time
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
from quant.reporting.flow_status import node_statuses  # noqa: E402
from quant.core.setup_store import apply_setup_updates, read_setup  # noqa: E402
from quant.risk.kill_switch import KillSwitch  # noqa: E402
from quant.core.trading_mode import request_mode, trading_mode_status  # noqa: E402
from quant.core.calendar import TradingCalendar  # noqa: E402
from quant.monitor.paper_loop import loop_status, now_ist, session_reason, spawn_loop, start_loop, stop_loop  # noqa: E402
from quant.execution.openalgo_client import OpenAlgoClient, http_transport  # noqa: E402
from quant.reporting.portfolio_view import HoldingsReader  # noqa: E402

TOKEN_ENV = "DASHBOARD_API_TOKEN"
TOKEN_HEADER = "X-Dashboard-Token"
REASON_MAX = 200
ACTOR = "dashboard-api"

DEFAULT_PATHS = {
    "ledger": ROOT / "data" / "paper" / "ledger.jsonl",
    "audit": ROOT / "data" / "paper" / "audit.jsonl",
    "kill": ROOT / "data" / "paper" / "kill_switch.json",
    "limits": ROOT / "config" / "risk_limits.yaml",
    "settings": ROOT / "config" / "settings.yaml",
    "strategies": ROOT / "config" / "strategies",
    "page": ROOT / "dashboard" / "main_dashboard.html",  # main dashboard (decision 0027)
    "flow": ROOT / "dashboard" / "assets" / "flow_svg.js",
    "loop": ROOT / "data" / "paper" / "loop",
    "holidays": ROOT / "config" / "calendar" / "holidays.csv",
    "mode_state": ROOT / "data" / "mode.json",          # requested mode (decision 0030)
    "live_approval": ROOT / "data" / "live_approval.json",  # human sign-off; written only by scripts/ops/approve_live.py
}
LOOP_INTERVAL_S = 60  # paper loop cycle (decision 0029)

API_INDEX = {
    "service": "Algo-Pro dashboard",
    "page": "/ui",
    "endpoints": [
        "/api/overview", "/api/positions", "/api/events", "/api/health", "/api/kill", "/api/setup",
        "POST /api/limits (token)", "POST /api/kill/trip (token)", "POST /api/kill/reset (token)",
        "POST /api/setup (token)", "/api/trading-mode", "POST /api/trading-mode (token; live needs approval)",
        "/api/loop", "POST /api/loop/start (token)", "POST /api/loop/stop (token)",
        "/api/portfolio",
    ],
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
    overview["trading_mode"] = trading_mode_status()
    holidays = Path(paths.get("holidays", DEFAULT_PATHS["holidays"]))
    cal = TradingCalendar.from_csv(holidays) if holidays.exists() else TradingCalendar(set(), verified=False)
    is_open, reason = session_reason(now_ist(), cal)
    overview["market"] = {"open": is_open, "reason": reason, "verified": cal.verified,
                          "holidays_loaded": len(cal.holidays)}
    overview["loop"] = loop_status(Path(paths.get("loop", DEFAULT_PATHS["loop"])), time.time(), LOOP_INTERVAL_S)
    overview["flow"] = node_statuses(overview)  # flow popup node colours (decision 0030)
    return overview


def create_app(paths: Optional[Dict[str, Path]] = None):
    """Build the FastAPI app. Import happens here so the module loads without FastAPI installed."""
    from fastapi import FastAPI, Request
    from fastapi.responses import FileResponse, JSONResponse

    load_env_file()  # reads the repo-root .env without overriding shell variables
    paths = {**DEFAULT_PATHS, **(paths or {})}
    app = FastAPI(title="Algo-Pro dashboard", version="0.1.0")
    # Read-only holdings. mode is not used: get_holdings sends no order.
    openalgo = OpenAlgoClient(os.environ.get("OPENALGO_URL", "http://127.0.0.1:5000"),
                              transport=http_transport, mode="analyze")
    portfolio = HoldingsReader(openalgo.get_holdings, OpenAlgoClient.api_key_from_env, time.time)

    def token_error(request: Request):
        """None when the token is good. Otherwise the error response (503 no server token, 401 wrong)."""
        expected = os.environ.get(TOKEN_ENV, "")
        if not expected:
            return JSONResponse({"error": f"writes disabled: {TOKEN_ENV} is not set"}, status_code=503)
        given = request.headers.get(TOKEN_HEADER, "")
        if not hmac.compare_digest(given.encode("utf-8"), expected.encode("utf-8")):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return None

    async def reason_from(request: Request):
        """(reason, None) when valid, else (None, 422 response)."""
        try:
            body = await request.json()
        except ValueError:  # JSON errors are ValueError subclasses
            body = None
        reason = body.get("reason") if isinstance(body, dict) else None
        if not isinstance(reason, str) or not 0 < len(reason.strip()) <= REASON_MAX:
            return None, JSONResponse({"error": f"reason is required (1-{REASON_MAX} characters)"},
                                      status_code=422)
        return reason.strip(), None

    @app.get("/")
    def index():
        return JSONResponse(API_INDEX)

    @app.get("/ui")
    def ui():
        """The one page: reads /api/overview every 2 s, writes through the token-protected POST routes."""
        return FileResponse(paths["page"], media_type="text/html")

    @app.get("/assets/flow_svg.js")
    def flow_asset():
        return FileResponse(paths["flow"], media_type="application/javascript")

    @app.post("/api/limits")
    async def set_limits(request: Request):
        """Save user-set risk limits. Needs the DASHBOARD_API_TOKEN header. Refused if no token is set."""
        denied = token_error(request)
        if denied is not None:
            return denied
        try:
            result = apply_limit_updates(paths["limits"], await request.json())
        except ValueError as exc:  # JSON errors are ValueError subclasses too
            return JSONResponse({"error": str(exc)}, status_code=422)
        AuditLog(paths["audit"]).append("limits_changed", {**result, "actor": ACTOR})
        return JSONResponse({"changed": result})

    @app.get("/api/kill")
    def kill_status():
        switch = KillSwitch(paths["kill"])
        return JSONResponse({"active": switch.active, "reason": switch.reason})

    @app.post("/api/kill/trip")
    async def kill_trip(request: Request):
        """Block new entries. Open positions are closed by the paper runner, never by this API."""
        denied = token_error(request)
        if denied is not None:
            return denied
        reason, bad = await reason_from(request)
        if bad is not None:
            return bad
        switch = KillSwitch(paths["kill"])
        switch.trip(reason, source=ACTOR)
        AuditLog(paths["audit"]).append("kill_switch", {"action": "trip", "reason": reason, "actor": ACTOR})
        return JSONResponse({"active": switch.active, "reason": switch.reason})

    @app.post("/api/kill/reset")
    async def kill_reset(request: Request):
        """Clear the kill switch. The reset is written to the override log."""
        denied = token_error(request)
        if denied is not None:
            return denied
        reason, bad = await reason_from(request)
        if bad is not None:
            return bad
        switch = KillSwitch(paths["kill"])
        switch.reset(ACTOR, reason, audit=AuditLog(paths["audit"]))
        return JSONResponse({"active": switch.active, "reason": switch.reason})

    @app.get("/api/setup")
    def setup_status():
        return JSONResponse(read_setup(paths["settings"], paths["strategies"], paths["limits"]))

    @app.post("/api/setup")
    async def setup_save(request: Request):
        """Save risk per trade, trades per day and strategy. Needs the token."""
        denied = token_error(request)
        if denied is not None:
            return denied
        try:
            body = await request.json()
            result = apply_setup_updates(paths["settings"], paths["strategies"], paths["limits"], body)
        except ValueError as exc:  # JSON errors are ValueError subclasses too
            return JSONResponse({"error": str(exc)}, status_code=422)
        audit = AuditLog(paths["audit"])
        audit.append("setup_changed", {**result, "actor": ACTOR})
        if "max_symbols_per_portfolio" in result["new"]:  # the cap is a risk limit
            audit.append("limits_changed", {"old": {"max_symbols_per_portfolio": result["old"]["max_symbols_per_portfolio"]},
                                            "new": {"max_symbols_per_portfolio": result["new"]["max_symbols_per_portfolio"]},
                                            "actor": ACTOR})
        return JSONResponse({"changed": result})

    @app.get("/api/trading-mode")
    def trading_mode():
        return JSONResponse(trading_mode_status(
            state_path=paths.get("mode_state"), approval_path=paths.get("live_approval"),
            limits_path=paths.get("limits")))

    @app.post("/api/trading-mode")
    async def set_trading_mode(request: Request):
        """Select paper or live (decision 0030). Live needs a valid LiveApproval. Needs the token.

        The mode is a REQUEST, never authority: authority is the approval file plus the
        risk gate. A live request without a valid approval is refused with 409 and audited,
        so a mis-click writes one audit row and nothing else.
        """
        denied = token_error(request)
        if denied is not None:
            return denied
        try:
            body = await request.json()
        except ValueError:
            body = None
        mode = body.get("mode") if isinstance(body, dict) else None
        audit = AuditLog(paths["audit"])
        accepted, message = request_mode(
            mode,
            state_path=paths.get("mode_state"), approval_path=paths.get("live_approval"),
            limits_path=paths.get("limits"), audit=audit)
        audit.append("trading_mode_requested",
                     {"requested": mode, "accepted": accepted, "message": message, "actor": ACTOR})
        if not accepted:
            return JSONResponse({"error": message}, status_code=409 if mode == "live" else 422)
        return JSONResponse(trading_mode_status(
            state_path=paths.get("mode_state"), approval_path=paths.get("live_approval"),
            limits_path=paths.get("limits")))

    @app.get("/api/loop")
    def loop_state():
        return JSONResponse(loop_status(paths["loop"], time.time(), LOOP_INTERVAL_S))

    @app.post("/api/loop/start")
    async def loop_start(request: Request):
        """Start the paper loop in the background. Refused while the kill switch is on. Needs the token."""
        denied = token_error(request)
        if denied is not None:
            return denied
        kill_active = KillSwitch(paths["kill"]).active
        ok, message = start_loop(paths["loop"], kill_active, LOOP_INTERVAL_S,
                                 lambda: spawn_loop(ROOT, paths["loop"], LOOP_INTERVAL_S))
        AuditLog(paths["audit"]).append("loop_start_requested", {"accepted": ok, "message": message, "actor": ACTOR})
        if not ok:
            return JSONResponse({"error": message}, status_code=409)
        return JSONResponse({"message": message})

    @app.post("/api/loop/stop")
    async def loop_stop(request: Request):
        """Ask the paper loop to stop. Does not close positions (the kill switch does that). Needs the token."""
        denied = token_error(request)
        if denied is not None:
            return denied
        ok, message = stop_loop(paths["loop"], LOOP_INTERVAL_S)
        AuditLog(paths["audit"]).append("loop_stop_requested", {"message": message, "actor": ACTOR})
        return JSONResponse({"message": message})

    @app.get("/api/portfolio")
    def portfolio_header():
        """Broker holdings for the portfolio popup. Read-only; the reader caches for 30 s."""
        return JSONResponse(portfolio.read())

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
