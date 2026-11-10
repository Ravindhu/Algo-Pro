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
import platform
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
from quant.core import dashboard_accounts as accounts  # noqa: E402
from quant.core import engine_control  # noqa: E402
from quant.core import schedule_settings as sched  # noqa: E402
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
    "schedule": ROOT / "data" / "schedule_settings.json",
    "users": ROOT / "data" / "dashboard_users.json",
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
        "/api/portfolio", "/api/infrastructure", "/api/account",
        "POST /api/account/login", "POST /api/account/logout",
        "/api/schedule", "POST /api/schedule (token or account)",
        "/api/engine", "POST /api/engine/start (token or account)",
    ],
}


def _installer_module():
    """scripts/ops/install_schedule.py loaded by path (it is a script, not a package)."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("install_schedule", ROOT / "scripts" / "ops" / "install_schedule.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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

    # ---------------- accounts, schedule, engine, infrastructure ----------------
    SESSION_COOKIE = "algo_session"

    def session_secret() -> str:
        return os.environ.get("DASHBOARD_SESSION_SECRET", "")

    def signed_in(request: Request) -> Optional[str]:
        return accounts.read_session(request.cookies.get(SESSION_COOKIE, ""), session_secret())

    def write_denied(request: Request):
        """None when the caller may write: a good token OR a signed-in account."""
        if signed_in(request):
            return None
        return token_error(request)

    @app.get("/api/account")
    def account_status(request: Request):
        return JSONResponse({"signed_in": signed_in(request),
                             "login_enabled": bool(session_secret())})

    @app.post("/api/account/login")
    async def account_login(request: Request):
        if not session_secret():
            return JSONResponse({"error": "account login is off: DASHBOARD_SESSION_SECRET is not set"},
                                status_code=503)
        try:
            body = await request.json()
        except ValueError:
            body = None
        name = body.get("name") if isinstance(body, dict) else None
        password = body.get("password") if isinstance(body, dict) else None
        if not isinstance(name, str) or not isinstance(password, str):
            return JSONResponse({"error": "name and password are required"}, status_code=422)
        ok, reason = accounts.verify_login(name, password, paths["users"])
        AuditLog(paths["audit"]).append("account_login", {"accepted": ok, "reason": reason,
                                                         "name": name[:32], "actor": ACTOR})
        if not ok:
            return JSONResponse({"error": reason}, status_code=401)
        response = JSONResponse({"signed_in": name})
        response.set_cookie(SESSION_COOKIE, accounts.make_session(name, session_secret()),
                            httponly=True, samesite="strict", max_age=accounts.SESSION_TTL)
        return response

    @app.post("/api/account/logout")
    def account_logout():
        response = JSONResponse({"signed_in": None})
        response.delete_cookie(SESSION_COOKIE)
        return response

    @app.get("/api/schedule")
    def schedule_get():
        settings, warning = sched.load(paths["schedule"])
        installer = _installer_module()
        plan = installer.build_plan(settings, detected=installer.detect_os(
            platform.system(), installer._has_systemd()), utc_offset_seconds=installer._utc_offset_seconds())
        return JSONResponse({"settings": settings.to_dict(), "warning": warning,
                             "errors": settings.errors(), "preview": plan})

    @app.post("/api/schedule")
    async def schedule_save(request: Request):
        """Save scheduler details and the engine toggle. OS/time changes need an install run (root)."""
        denied = write_denied(request)
        if denied is not None:
            return denied
        try:
            body = await request.json()
        except ValueError:
            body = None
        if not isinstance(body, dict):
            return JSONResponse({"error": "JSON object expected"}, status_code=422)
        current, _ = sched.load(paths["schedule"])
        fields = {k: body[k] for k in sched.ScheduleSettings.__dataclass_fields__ if k in body}
        candidate = sched.ScheduleSettings(**{**current.to_dict(), **fields})
        if candidate.errors():
            return JSONResponse({"error": "; ".join(candidate.errors())}, status_code=422)
        sched.save(candidate, paths["schedule"])
        AuditLog(paths["audit"]).append("schedule_saved", {"settings": candidate.to_dict(), "actor": ACTOR})
        return JSONResponse({"saved": candidate.to_dict(),
                             "note": "saved. Auto engine start applies at the next run. OS or start time changes need scripts/ops/install_schedule.py --apply once on this machine."})

    @app.get("/api/engine")
    def engine_status():
        pid = engine_control.running_pid()
        return JSONResponse({"running": pid is not None, "pid": pid})

    @app.post("/api/engine/start")
    async def engine_start(request: Request):
        """Button: broker login, then engine start. The supervisor's own gates still decide whether anything trades."""
        denied = write_denied(request)
        if denied is not None:
            return denied
        kill_active = KillSwitch(paths["kill"]).active
        ok, message, pid = engine_control.request_manual_start(kill_active=kill_active)
        AuditLog(paths["audit"]).append("engine_start_requested", {"accepted": ok, "message": message,
                                                                  "actor": ACTOR})
        if not ok:
            return JSONResponse({"error": message}, status_code=409)
        return JSONResponse({"message": message, "pid": pid})

    @app.get("/api/infrastructure")
    def infrastructure():
        """Plain list of what the desk depends on. Read-only; a closed port is a fact, not an error."""
        import socket
        from urllib.parse import urlparse

        def port_up(url: str, default_port: int) -> bool:
            parsed = urlparse(url)
            try:
                with socket.create_connection((parsed.hostname or "127.0.0.1", parsed.port or default_port),
                                              timeout=0.5):
                    return True
            except OSError:
                return False

        settings, _ = sched.load(paths["schedule"])
        installer = _installer_module()
        detected = installer.detect_os(platform.system(), installer._has_systemd())
        cron_present = installer.CRON_FILE.exists()
        users_present = paths["users"].is_file()
        items = [
            {"name": "Dashboard server", "ok": True, "detail": "this page is being served"},
            {"name": "OpenAlgo platform", "ok": port_up(os.environ.get("OPENALGO_URL", "http://127.0.0.1:5000"), 5000),
             "detail": "port 5000"},
            {"name": "WebSocket proxy", "ok": port_up("http://127.0.0.1:8765", 8765), "detail": "port 8765"},
            {"name": "Kill switch", "ok": not KillSwitch(paths["kill"]).active,
             "detail": "tripped = engine blocked"},
            {"name": "Engine", "ok": engine_control.running_pid() is not None,
             "detail": "started by the button or the schedule"},
            {"name": "Machine OS", "ok": detected != "unsupported", "detail": detected},
            {"name": "Scheduler", "ok": detected != "unsupported" and settings.valid,
             "detail": f"{detected} · start {settings.start_time} IST · broker {settings.broker}"},
            {"name": "Auto engine start", "ok": settings.engine_start,
             "detail": "ON: engine starts after login" if settings.engine_start else "OFF: use the START ENGINE button"},
            {"name": "Cron job installed", "ok": cron_present if detected == "ubuntu" else None,
             "detail": "Ubuntu only" if detected != "ubuntu" else ("present" if cron_present else "absent")},
            {"name": "Dashboard accounts", "ok": users_present, "detail": "data/dashboard_users.json"},
            {"name": "Account login secret", "ok": bool(session_secret()),
             "detail": "DASHBOARD_SESSION_SECRET"},
            {"name": "Write token", "ok": bool(os.environ.get(TOKEN_ENV)), "detail": TOKEN_ENV},
        ]
        return JSONResponse({"items": items})

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
