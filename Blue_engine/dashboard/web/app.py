"""
app.py - FastAPI application for the POV trading dashboard.

Routes (all GET, all read-only):

    GET /                      the single-page dashboard
    GET /healthz               liveness probe
    GET /api/sections          the twelve header buttons
    GET /api/sections/{key}    one popup's details
    GET /api/flow             main neural shell (whole system)
    GET /api/engines          engine list with live status
    GET /api/engines/{key}    one engine's neural shell and details

Nothing here places an order. The dashboard only reads snapshots and the
ledger. Bind to 127.0.0.1 unless you have put the port behind your own auth.

FastAPI is imported inside :func:`create_app` so the section builders in
``sections.py`` stay importable without the web stack.
"""
from __future__ import annotations

from pathlib import Path

from . import flow, sections

_STATIC = Path(__file__).resolve().parent / "static"


def create_app():
    """Build the FastAPI application."""
    from fastapi import FastAPI, HTTPException
    from fastapi.responses import FileResponse, JSONResponse
    from fastapi.staticfiles import StaticFiles

    app = FastAPI(title="POV Terminal", version="2.0.0",
                  docs_url=None, redoc_url=None, openapi_url=None)
    app.mount("/static", StaticFiles(directory=str(_STATIC)), name="static")

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(_STATIC / "index.html", media_type="text/html")

    @app.get("/healthz")
    def healthz():
        return {"status": "ok"}

    @app.get("/api/sections")
    def list_sections():
        return {"sections": sections.section_titles()}

    @app.get("/api/sections/{key}")
    def section(key: str):
        payload = sections.build_section(key)
        if payload is None:
            raise HTTPException(status_code=404, detail=f"unknown section {key!r}")
        return JSONResponse(payload)

    @app.get("/api/flow")
    def get_flow():
        return JSONResponse(flow.build_flow())

    @app.get("/api/engines")
    def list_engines():
        return {"engines": [flow.engine_summary(k) for k in flow.ENGINES]}

    @app.get("/api/engines/{key}")
    def engine(key: str):
        data = flow.build_engine(key)
        if data is None:
            raise HTTPException(status_code=404, detail=f"unknown engine {key!r}")
        return JSONResponse(data)

    return app
