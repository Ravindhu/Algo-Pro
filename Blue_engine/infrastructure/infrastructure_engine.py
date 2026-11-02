"""
infrastructure.py - the Infrastructure engine.

The last stage of the pipeline. It owns *operational readiness*: are the
platform, broker session, dashboard and host all healthy enough to trade?

Four checks, each independently reported:

  1. openalgo   : platform reachable + mode readable via the SDK
                  (``client.analyzerstatus()``). Read-only; never toggles mode.
  2. brokers    : broker session / funds / auth health via the SDK
                  (``client.funds()``, ``client.orderbook()``).
  3. dashboard  : whether the Plotly dashboard process/port is up and the
                  dashboard module imports cleanly.
  4. servers    : host resource sanity - port availability for the platform
                  and dashboard, plus an optional disk/uptime probe.

Design rules
------------
* Every check is FAIL-SOFT: a raised exception becomes a ``{"ok": False,
  "detail": ...}`` entry, never an engine failure.
* The engine is *read-only* against the platform. It never places, cancels or
  toggles anything. Its only purpose is to gate the pipeline with a clear
  go/no-go and produce an operator-facing health report.
* No OpenAlgo-internal imports: only the public SDK client (injected via
  context) and the standard library (socket, os, importlib).
"""
from __future__ import annotations

import importlib
import os
import socket
from typing import Any, Callable, Dict, List, Optional, Tuple

from ..core.base import BaseEngine, EngineResult, EngineStage


def _port_open(host: str, port: int, timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except OSError:
        return False


class InfrastructureEngine(BaseEngine):
    """Gate the pipeline on platform / broker / dashboard / host health."""

    name = "infrastructure"
    stage = EngineStage.INFRASTRUCTURE

    def __init__(self, config: Optional[Dict[str, Any]] = None,
                 provider: Optional[Callable[[], Dict[str, Any]]] = None,
                 **kwargs: Any):
        super().__init__(config, provider, kwargs.get("log"))
        self.openalgo_host: str = str(
            self.config.get("openalgo_host", "127.0.0.1"))
        self.openalgo_port: int = int(self.config.get("openalgo_port", 5000))
        self.dash_port: int = int(self.config.get("dash_port", 8050))
        self.ws_port: int = int(self.config.get("ws_port", 8765))
        #: When True, an unreachable dashboard is a hard failure. Default
        #: False: the dashboard is an operator convenience, not a trade gate.
        self.dashboard_required: bool = bool(
            self.config.get("dashboard_required", False))

    # ---- 1. openalgo --------------------------------------------------- #
    def _check_openalgo(self, client: Any) -> Dict[str, Any]:
        reachable = _port_open(self.openalgo_host, self.openalgo_port)
        mode = None
        if client is not None and hasattr(client, "analyzerstatus"):
            try:
                resp = client.analyzerstatus()
                if isinstance(resp, dict) and resp.get("status") == "success":
                    mode = (resp.get("data") or {}).get("mode")
            except Exception as exc:  # noqa: BLE001
                return {"ok": False,
                        "detail": f"analyzerstatus failed: {exc!r}",
                        "port_open": reachable}
        return {
            "ok": reachable,
            "port_open": reachable,
            "mode": mode,
            "host": self.openalgo_host,
            "port": self.openalgo_port,
        }

    # ---- 2. brokers ---------------------------------------------------- #
    def _check_broker(self, client: Any) -> Dict[str, Any]:
        if client is None:
            return {"ok": False, "detail": "no SDK client in context"}
        detail: Dict[str, Any] = {"ok": True}
        for method, key in (("funds", "funds"), ("orderbook", "orderbook")):
            if not hasattr(client, method):
                detail[key] = "unavailable"
                continue
            try:
                resp = getattr(client, method)()
                ok = isinstance(resp, dict) and resp.get("status") == "success"
                detail[key] = "ok" if ok else resp
                if not ok:
                    detail["ok"] = False
            except Exception as exc:  # noqa: BLE001
                detail[key] = repr(exc)
                detail["ok"] = False
        return detail

    # ---- 3. dashboard -------------------------------------------------- #
    def _check_dashboard(self) -> Dict[str, Any]:
        port_open = _port_open("127.0.0.1", self.dash_port)
        imports_ok = True
        import_error = None
        try:
            importlib.import_module("blue_engine.dashboard.dash_app")
        except Exception as exc:  # noqa: BLE001
            imports_ok = False
            import_error = repr(exc)
        return {
            "ok": imports_ok and (port_open or not self.dashboard_required),
            "port_open": port_open,
            "imports_ok": imports_ok,
            "import_error": import_error,
            "port": self.dash_port,
        }

    # ---- 4. servers ---------------------------------------------------- #
    def _check_servers(self) -> Dict[str, Any]:
        ws_open = _port_open("127.0.0.1", self.ws_port)
        disk = None
        try:
            import shutil
            total, used, free = shutil.disk_usage(os.getcwd())
            disk = {"total_gb": round(total / 1e9, 1),
                    "free_gb": round(free / 1e9, 1),
                    "used_pct": round(used / total * 100, 1)}
        except Exception:  # noqa: BLE001
            pass
        healthy = disk is None or disk["used_pct"] < 95.0
        return {
            "ok": healthy,
            "websocket_port_open": ws_open,
            "ws_port": self.ws_port,
            "disk": disk,
        }

    # ---- main ---------------------------------------------------------- #
    def _run(self, context: Dict[str, Any]) -> EngineResult:
        warnings: List[str] = []
        client = context.get("client")

        openalgo = self._check_openalgo(client)
        broker = self._check_broker(client)
        dashboard = self._check_dashboard()
        servers = self._check_servers()

        checks = {"openalgo": openalgo, "broker": broker,
                  "dashboard": dashboard, "servers": servers}

        # Hard gates: openalgo reachable + broker session healthy. The
        # dashboard is soft unless dashboard_required is set.
        hard_keys = ["openalgo", "broker", "servers"]
        if self.dashboard_required:
            hard_keys.append("dashboard")
        ready = all(checks[k].get("ok") for k in hard_keys)

        if not openalgo.get("ok"):
            warnings.append("OpenAlgo platform not reachable")
        if not broker.get("ok"):
            warnings.append("broker session/funds check failed")
        if not dashboard.get("ok"):
            warnings.append("dashboard down (non-fatal)")
        if servers.get("disk") and servers["disk"]["used_pct"] >= 90:
            warnings.append("host disk above 90% used")

        self.log(f"ready={ready} "
                 f"openalgo={openalgo.get('ok')} broker={broker.get('ok')} "
                 f"dash={dashboard.get('ok')} servers={servers.get('ok')}")

        return EngineResult(
            engine=self.name, ok=True,   # health report never fails the engine
            data={
                "ready_to_trade": ready,
                "hard_gates": hard_keys,
                "checks": checks,
            },
            warnings=warnings,
        )


__all__ = ["InfrastructureEngine"]