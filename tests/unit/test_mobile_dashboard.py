"""Phone dashboard (docs/MOBILE_DASHBOARD_PLAN.md, phases M1, M2, M4, M5).

Checks the page's phone setup, the install files and the service worker rules.
The service worker must never cache live API data.
"""
import json
import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
PAGE = ROOT / "dashboard" / "main_dashboard.html"
ASSETS = ROOT / "dashboard" / "assets"


def test_page_has_phone_viewport_and_install_links():
    text = PAGE.read_text(encoding="utf-8")
    assert 'name="viewport" content="width=device-width, initial-scale=1' in text
    assert '<link rel="manifest" href="/manifest.webmanifest">' in text
    assert '<script src="/assets/pwa.js"></script>' in text


def test_page_has_phone_layout_breakpoint():
    text = PAGE.read_text(encoding="utf-8")
    assert "@media (max-width: 600px)" in text


def test_manifest_is_valid_and_standalone():
    data = json.loads((ASSETS / "manifest.webmanifest").read_text(encoding="utf-8"))
    assert data["start_url"] == "/ui"
    assert data["display"] == "standalone"
    assert data["icons"] and data["icons"][0]["src"] == "/assets/icon.svg"


def test_service_worker_never_caches_api():
    text = (ASSETS / "sw.js").read_text(encoding="utf-8")
    assert 'url.pathname.startsWith("/api/")' in text
    assert 'event.request.method !== "GET"' in text
    # only the fixed shell list is cached
    urls = re.search(r"SHELL_URLS = \[(.*?)\]", text, re.S).group(1)
    assert "/api" not in urls


def test_phone_view_hides_kill_reset_and_has_no_engine_stop():
    text = PAGE.read_text(encoding="utf-8")
    assert 'id="killreset"' in text  # desktop control still exists
    assert 'id="engstop"' not in text
    phone = text[text.index("@media (max-width: 600px)"):]
    phone = phone[:phone.index("}\n  }") + 4]
    assert "#killreset" in phone and "display: none" in phone


def test_pwa_routes_are_served():
    pytest.importorskip("fastapi")
    pytest.importorskip("fastapi.testclient")
    from fastapi.testclient import TestClient
    import sys
    sys.path.insert(0, str(ROOT / "dashboard"))
    import app as dashboard_app  # noqa: E402

    client = TestClient(dashboard_app.create_app())
    assert client.get("/manifest.webmanifest").status_code == 200
    sw = client.get("/sw.js")
    assert sw.status_code == 200 and sw.headers["cache-control"] == "no-cache"
    assert client.get("/assets/pwa.js").status_code == 200
    assert client.get("/assets/icon.svg").status_code == 200
    assert client.get("/ui").status_code == 200
