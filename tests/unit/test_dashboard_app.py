"""FastAPI routes. Runs only where FastAPI is installed (owner machine). Read-only: no POST routes."""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "dashboard"))

fastapi = pytest.importorskip("fastapi")
testclient = pytest.importorskip("fastapi.testclient")


@pytest.fixture
def client(tmp_path):
    from app import create_app

    page = tmp_path / "terminal.html"
    page.write_text("<html>ok</html>", encoding="utf-8")
    paths = {
        "ledger": tmp_path / "ledger.jsonl", "audit": tmp_path / "audit.jsonl",
        "kill": tmp_path / "kill.json", "limits": tmp_path / "none.yaml",
        "settings": tmp_path / "none.yaml", "page": page,
    }
    return testclient.TestClient(create_app(paths))


def test_overview_and_health(client):
    assert client.get("/api/overview").status_code == 200
    h = client.get("/api/health").json()
    assert h["feed"]["status"] == "not connected"


def test_index_serves_page(client):
    assert "ok" in client.get("/").text


def test_no_write_routes(client):
    for method in ("post", "put", "delete"):
        assert getattr(client, method)("/api/overview").status_code in (404, 405)
