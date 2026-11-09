"""Analyzer mode (decision 0026, docs/ANALYZER_MODE_PLAN.md): guards, broker results, end-to-end run.

Flow stage: OpenAlgo
Status: PHASE 1 IMPLEMENTED (analyze mode, AZ1/AZ2). Fake OpenAlgo only; real OpenAlgo run is owner-only (AZ3).

Two fakes are used:
  - a fake transport (list of calls) for guard and broker rules
  - a real local HTTP server speaking OpenAlgo's JSON shapes, for the full path through http_transport
"""
import json
import sys
import pathlib
import threading
from datetime import date
from http.server import BaseHTTPRequestHandler, HTTPServer

import pandas as pd
import pytest

from quant.core.types import GateDecision, TradeIntent
from quant.execution.openalgo_client import (
    AnalyzeBroker, LiveApproval, OpenAlgoClient, file_sha256, http_transport,
)

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
import run_paper  # noqa: E402

APPROVED = GateDecision("I1", True, ())
ON = {"status": "success", "data": {"analyze_mode": True}}
OFF = {"status": "success", "data": {"analyze_mode": False}}
PLACED = {"status": "success", "orderid": "ORD1"}


def intent(intent_id="I1", kind="entry"):
    return TradeIntent(intent_id=intent_id, strategy_id="S1", symbol="INFY", side="BUY", qty=10,
                       ref_price=1500.0, kind=kind, lot_size=1)


class Fake:
    """Scripted OpenAlgo transport. Routes by path; records every call."""

    def __init__(self, analyzer=ON, place=PLACED, status=None, raise_on_place=None):
        self.analyzer, self.place_reply, self.status_reply = analyzer, place, status
        self.raise_on_place = raise_on_place
        self.calls = []

    def __call__(self, url, payload):
        self.calls.append((url.rsplit("/", 1)[-1], dict(payload)))
        path = url.rsplit("/api/v1/", 1)[-1]
        if path == "analyzer":
            return self.analyzer
        if path == "placeorder":
            if self.raise_on_place:
                raise self.raise_on_place
            return self.place_reply
        if path == "orderstatus":
            return self.status_reply
        return {"status": "success"}

    def sent(self):
        return [c for c in self.calls if c[0] in ("placeorder", "cancelorder")]


def analyze_client(fake):
    return OpenAlgoClient("http://x", fake, mode="analyze")


# ---- client guard -------------------------------------------------------------

def test_analyze_sends_when_analyzer_on_and_gate_approved():
    fake = Fake()
    reply = analyze_client(fake).place(intent(), "key", APPROVED)
    assert reply == PLACED
    assert [c[0] for c in fake.calls] == ["analyzer", "placeorder"]
    assert fake.calls[1][1]["symbol"] == "INFY" and fake.calls[1][1]["quantity"] == 10


@pytest.mark.parametrize("reply", [
    OFF,                                              # analyzer is off: could reach the broker
    {"status": "error", "message": "bad key"},        # error reply
    {"status": "success"},                            # no data block
    {"status": "success", "data": {"analyze_mode": "true"}},  # not a real boolean
    {"status": "success", "data": {}},
    "not a dict",
])
def test_analyze_refused_unless_analyzer_confirmed_on(reply):
    fake = Fake(analyzer=reply)
    with pytest.raises(PermissionError, match="analyze refused"):
        analyze_client(fake).place(intent(), "key", APPROVED)
    assert fake.sent() == []


def test_analyze_refused_without_key_and_nothing_sent():
    fake = Fake()
    with pytest.raises(PermissionError, match="analyze refused"):
        analyze_client(fake).place(intent(), "", APPROVED)
    assert fake.calls == []


def test_analyze_refused_without_approved_decision():
    fake = Fake()
    rejected = GateDecision("I1", False, ("blocked",))
    with pytest.raises(PermissionError, match="no approved gate decision"):
        analyze_client(fake).place(intent(), "key", rejected)
    assert fake.sent() == []


def test_analyze_refused_when_decision_is_for_another_intent():
    fake = Fake()
    with pytest.raises(PermissionError, match="does not match"):
        analyze_client(fake).place(intent("I2"), "key", APPROVED)
    assert fake.sent() == []


def test_analyze_needs_no_live_approval(tmp_path):
    fake = Fake()
    limits = tmp_path / "risk_limits.yaml"
    limits.write_text("max_order_quantity: 10\n", encoding="utf-8")
    client = OpenAlgoClient("http://x", fake, mode="analyze", live_approval=None, limits_path=limits)
    client.place(intent(), "key", APPROVED)
    assert fake.sent()


def test_analyze_place_child_limit_and_market():
    fake = Fake()
    client = analyze_client(fake)
    client.place_child(intent(), "key", APPROVED, price_type="LIMIT", limit_price=1499.5)
    client.place_child(intent(), "key", APPROVED)
    sent = fake.sent()
    assert sent[0][1]["pricetype"] == "LIMIT" and sent[0][1]["price"] == 1499.5
    assert sent[1][1]["pricetype"] == "MARKET" and "price" not in sent[1][1]


def test_analyze_cancel_needs_analyzer_on():
    fake = Fake(analyzer=OFF)
    with pytest.raises(PermissionError):
        analyze_client(fake).cancel("ORD1", "S1", "key")
    assert fake.sent() == []
    fake2 = Fake()
    analyze_client(fake2).cancel("ORD1", "S1", "key")
    assert fake2.calls[-1][0] == "cancelorder"


def test_paper_mode_still_refused_by_client():
    with pytest.raises(PermissionError, match="mode is live"):
        OpenAlgoClient("http://x", Fake(), mode="paper").place(intent(), "key", APPROVED)


def test_live_mode_unchanged_by_analyze_path(tmp_path):
    """Live still needs a LiveApproval; the analyzer query is never used in live mode."""
    fake = Fake()
    limits = tmp_path / "risk_limits.yaml"
    limits.write_text("max_order_quantity: 10\n", encoding="utf-8")
    client = OpenAlgoClient("http://x", fake, mode="live", live_approval=None, limits_path=limits)
    with pytest.raises(PermissionError, match="human approval"):
        client.place(intent(), "key", APPROVED)
    assert fake.calls == []
    approval = LiveApproval("owner", date(2026, 11, 6), file_sha256(limits))
    OpenAlgoClient("http://x", fake, mode="live", live_approval=approval, limits_path=limits).place(
        intent(), "key", APPROVED)
    assert [c[0] for c in fake.calls] == ["placeorder"]


def test_get_order_status_payload():
    fake = Fake(status={"status": "success", "data": {"order_status": "complete", "average_price": 1.0}})
    analyze_client(fake).get_order_status("key", "ORD1", "S1")
    assert fake.calls[-1] == ("orderstatus", {"apikey": "key", "orderid": "ORD1", "strategy": "S1"})


# ---- broker results ----------------------------------------------------------

def complete(avg=1501.25):
    return {"status": "success", "data": {"order_status": "complete", "average_price": avg}}


def test_broker_filled_when_complete_with_price():
    fake = Fake(status=complete())
    res = AnalyzeBroker(analyze_client(fake), "key").place(intent(), APPROVED)
    assert res["status"] == "FILLED" and res["order_id"] == "ORD1"
    assert res["filled_qty"] == 10 and res["avg_price"] == 1501.25


def test_broker_accepts_flat_status_reply():
    fake = Fake(status={"order_status": "Complete", "average_price": 1500.0})
    assert AnalyzeBroker(analyze_client(fake), "key").place(intent(), APPROVED)["status"] == "FILLED"


@pytest.mark.parametrize("state", ["rejected", "cancelled", "canceled"])
def test_broker_rejected_on_terminal_bad_states(state):
    fake = Fake(status={"status": "success", "data": {"order_status": state, "average_price": 0}})
    assert AnalyzeBroker(analyze_client(fake), "key").place(intent(), APPROVED)["status"] == "REJECTED"


@pytest.mark.parametrize("status", [
    {"status": "success", "data": {"order_status": "open", "average_price": 0}},
    {"status": "success", "data": {"order_status": "complete", "average_price": 0}},
    {"status": "success", "data": {"order_status": "complete"}},
    {"status": "success", "data": {"order_status": "complete", "average_price": float("nan")}},
    {"status": "success", "data": {}},
])
def test_broker_unknown_is_never_a_fill(status):
    fake = Fake(status=status)
    res = AnalyzeBroker(analyze_client(fake), "key").place(intent(), APPROVED)
    assert res["status"] == "UNKNOWN" and res["avg_price"] is None and res["filled_qty"] == 0


def test_broker_refused_guard_is_rejected_and_no_order_sent():
    fake = Fake(analyzer=OFF)
    res = AnalyzeBroker(analyze_client(fake), "key").place(intent(), APPROVED)
    assert res["status"] == "REJECTED" and "analyze refused" in res["reason"]
    assert fake.sent() == []


def test_broker_order_error_reply_is_rejected():
    fake = Fake(place={"status": "error", "message": "Insufficient funds"})
    res = AnalyzeBroker(analyze_client(fake), "key").place(intent(), APPROVED)
    assert res["status"] == "REJECTED" and res["reason"] == "Insufficient funds"


def test_broker_network_error_on_send_is_unknown():
    fake = Fake(raise_on_place=ConnectionError("timed out"))
    res = AnalyzeBroker(analyze_client(fake), "key").place(intent(), APPROVED)
    assert res["status"] == "UNKNOWN"


def test_broker_status_read_error_keeps_order_id_unknown():
    class BadStatus(Fake):
        def __call__(self, url, payload):
            if url.endswith("/orderstatus"):
                raise ConnectionError("down")
            return super().__call__(url, payload)

    res = AnalyzeBroker(analyze_client(BadStatus()), "key").place(intent(), APPROVED)
    assert res["status"] == "UNKNOWN" and res["order_id"] == "ORD1"


def test_broker_needs_key():
    with pytest.raises(ValueError):
        AnalyzeBroker(analyze_client(Fake()), "")


# ---- end-to-end run ------------------------------------------------------------

def _limits(tmp_path):
    p = tmp_path / "limits.yaml"
    p.write_text(
        "max_order_quantity: 1000\ncapital_inr: 100000000\nmax_order_notional_pct: 0.05\nmax_open_exposure_pct: 0.2\n"
        "max_daily_loss_pct: 0.001\nmax_strategy_loss_pct: 0.0005\nmax_adv_pct: 0.05\nmax_symbols_per_portfolio: 10\n"
    )
    return p


def _signals(tmp_path, rows):
    base = {"strategy_id": "S1", "side": "BUY", "ref_price": 100.0, "kind": "entry", "lot_size": 1,
            "prev_close": 99.0, "circuit_pct": 0.2, "adv_shares": 1e6, "asm_gsm": False, "symbol": "A"}
    sig = tmp_path / "signals.csv"
    pd.DataFrame([{**base, **r} for r in rows]).to_csv(sig, index=False)
    return sig


def test_run_with_fake_analyzer_fills_and_restart_is_safe(tmp_path):
    limits = _limits(tmp_path)
    sig = _signals(tmp_path, [{"intent_id": "I1", "qty": 10}])
    fake = Fake(status=complete(100.0))
    broker = AnalyzeBroker(analyze_client(fake), "key")
    out = tmp_path / "analyze"
    first = run_paper.run(sig, limits, out, broker=broker)
    assert first["approved"] == 1 and first["filled"] == 1 and first["positions"] == {"A": 10}
    assert first["audit_verified"]
    second = run_paper.run(sig, limits, out, broker=broker)
    assert second["skipped_duplicates"] == 1 and second["filled"] == 0
    assert len(fake.sent()) == 1  # no duplicate order after restart


def test_run_analyzer_off_sends_nothing_and_no_position(tmp_path):
    limits = _limits(tmp_path)
    sig = _signals(tmp_path, [{"intent_id": "I1", "qty": 10}])
    fake = Fake(analyzer=OFF)
    out = tmp_path / "analyze"
    res = run_paper.run(sig, limits, out, broker=AnalyzeBroker(analyze_client(fake), "key"))
    assert res["rejected"] == 1 and res["filled"] == 0
    assert res["positions"] == {} and fake.sent() == []
    assert res["audit_verified"]


def test_run_unknown_result_creates_no_position(tmp_path):
    limits = _limits(tmp_path)
    sig = _signals(tmp_path, [{"intent_id": "I1", "qty": 10}])
    fake = Fake(status={"status": "success", "data": {"order_status": "open", "average_price": 0}})
    res = run_paper.run(sig, limits, tmp_path / "a", broker=AnalyzeBroker(analyze_client(fake), "key"))
    assert res["unknown"] == 1 and res["positions"] == {}


def test_run_default_broker_is_still_paper(tmp_path):
    res = run_paper.run(_signals(tmp_path, [{"intent_id": "I1", "qty": 10}]), _limits(tmp_path), tmp_path / "p")
    assert res["filled"] == 1 and res["positions"] == {"A": 10}


# ---- real HTTP round trip through http_transport ---------------------------------

class _OpenAlgoHandler(BaseHTTPRequestHandler):
    """Speaks OpenAlgo's JSON shapes for the three analyze routes."""

    def log_message(self, *args):  # keep test output quiet
        pass

    def do_POST(self):
        srv = self.server
        length = int(self.headers.get("Content-Length", 0))
        payload = json.loads(self.rfile.read(length).decode("utf-8"))
        route = self.path.rsplit("/api/v1/", 1)[-1]
        srv.calls.append((route, payload))
        if route == "analyzer":
            body, code = (ON if srv.analyze_on else OFF), 200
        elif route == "placeorder":
            body, code = PLACED, 200
        elif route == "orderstatus":
            body, code = {"status": "success", "data": {"order_status": "complete", "average_price": 100.0}}, 200
        else:
            body, code = {"status": "error", "message": "unknown route"}, 404
        raw = json.dumps(body).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


@pytest.fixture
def openalgo_server(monkeypatch):
    # Local test server: never go through an outside proxy.
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    srv = HTTPServer(("127.0.0.1", 0), _OpenAlgoHandler)
    srv.calls, srv.analyze_on = [], True
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        yield srv, f"http://127.0.0.1:{srv.server_address[1]}"
    finally:
        srv.shutdown()
        srv.server_close()


def test_http_round_trip_full_run_in_analyze_mode(tmp_path, openalgo_server):
    srv, base = openalgo_server
    client = OpenAlgoClient(base, http_transport, mode="analyze")
    res = run_paper.run(_signals(tmp_path, [{"intent_id": "I1", "qty": 10}]), _limits(tmp_path), tmp_path / "h",
                        broker=AnalyzeBroker(client, "key"))
    assert res["filled"] == 1 and res["positions"] == {"A": 10} and res["audit_verified"]
    assert [r for r, _ in srv.calls] == ["analyzer", "placeorder", "orderstatus"]
    assert srv.calls[1][1]["quantity"] == 10


def test_http_round_trip_refuses_when_analyzer_off(tmp_path, openalgo_server):
    srv, base = openalgo_server
    srv.analyze_on = False
    client = OpenAlgoClient(base, http_transport, mode="analyze")
    res = run_paper.run(_signals(tmp_path, [{"intent_id": "I1", "qty": 10}]), _limits(tmp_path), tmp_path / "h2",
                        broker=AnalyzeBroker(client, "key"))
    assert res["rejected"] == 1 and res["positions"] == {}
    assert [r for r, _ in srv.calls] == ["analyzer"]  # no order route was ever called


def test_http_transport_returns_json_error_body(openalgo_server):
    srv, base = openalgo_server
    body = http_transport(base + "/api/v1/nope", {"apikey": "k"})
    assert body["status"] == "error"
