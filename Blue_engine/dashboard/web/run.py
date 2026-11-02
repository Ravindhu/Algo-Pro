"""
run.py - start the FastAPI dashboard with uvicorn.

    python -m blue_engine.dashboard.web.run

Environment:
    DASHBOARD_HOST   bind host (default 127.0.0.1)
    DASHBOARD_PORT   bind port (default 8050). DASH_PORT and POV_DASH_PORT
                     are accepted as legacy names.
"""
from __future__ import annotations

import os
import platform

# Windows platform-probe guard. pandas calls platform.win32_ver(), which can
# block forever in a detached or redirected console. The arch of a running
# 64-bit interpreter is known directly, so answer it and skip the subprocess.
if os.name == "nt":
    _ARCH = os.environ.get("PROCESSOR_ARCHITECTURE", "") or (
        "ARM64" if "ARM64" in platform.version().upper() else "AMD64")
    platform.machine = lambda: _ARCH                       # type: ignore[assignment]
    platform.win32_ver = lambda *a, **k: ("10", "", "", "")  # type: ignore[assignment]
    platform.uname = lambda *a, **k: platform.uname_result(  # type: ignore[assignment]
        "Windows", platform.node(), "10", "", "", _ARCH)


def main() -> None:
    import uvicorn

    host = os.getenv("DASHBOARD_HOST", "127.0.0.1")
    port = int(os.getenv("DASHBOARD_PORT")
               or os.getenv("DASH_PORT")
               or os.getenv("POV_DASH_PORT")
               or "8050")
    uvicorn.run("blue_engine.dashboard.web.app:create_app", factory=True,
                host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
