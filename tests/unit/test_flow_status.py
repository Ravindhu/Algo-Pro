"""Flow popup node status (decision 0030). Pure rules plus a check that every label exists in the v5 SVG.

Flow stage: Dashboard. Rules use current state only; old audit events never colour a node.
"""
import pathlib
import sys
import xml.etree.ElementTree as ET

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from quant.reporting.flow_status import node_statuses  # noqa: E402

SVG_NS = "{http://www.w3.org/2000/svg}"


def clean(**over):
    o = {"kill_switch": {"active": False, "reason": ""}, "mode": "normal", "unset_limits": [],
         "events": [], "loop": {"status": "stopped", "last_error": None},
         "market": {"verified": True}, "feed": {"status": "connected", "note": ""}}
    o.update(over)
    return o


def test_clean_state_flags_nothing():
    assert node_statuses(clean()) == {}


def test_active_kill_switch_is_red_on_its_node():
    out = node_statuses(clean(kill_switch={"active": True, "reason": "manual"}))
    assert out["Kill switch"]["status"] == "err"
    assert "manual" in out["Kill switch"]["message"]


def test_old_kill_event_does_not_stay_red_after_reset():
    old = [{"severity": "CRITICAL", "kind": "kill_switch", "message": "old trip", "ts": None}]
    assert "Kill switch" not in node_statuses(clean(events=old))


def test_halt_mode_is_red_and_reduce_only_is_amber():
    assert node_statuses(clean(mode="halt-all"))["Safe mode controller"]["status"] == "err"
    assert node_statuses(clean(mode="reduce-only"))["Safe mode controller"]["status"] == "warn"


def test_loop_error_is_red_on_paper_stage():
    out = node_statuses(clean(loop={"status": "stopped", "last_error": "broker timeout"}))
    assert out["Paper trading stage"]["status"] == "err"
    assert "broker timeout" in out["Paper trading stage"]["message"]


def test_unset_limits_make_their_own_nodes_amber():
    out = node_statuses(clean(unset_limits=["max_adv_pct", "max_daily_loss", "max_order_quantity"]))
    assert out["Liquidity cap"]["status"] == "warn"
    assert out["Loss budget breaker"]["status"] == "warn"
    assert out["Pre-trade checks"]["status"] == "warn"
    assert "Risk gate" not in out


def test_unverified_calendar_and_disconnected_feed_are_amber():
    out = node_statuses(clean(market={"verified": False}, feed={"status": "not connected", "note": "none yet"}))
    assert out["Trading calendar"]["status"] == "warn"
    assert out["Broker feeds"]["status"] == "warn"
    assert out["Feed health monitor"]["status"] == "warn"


def test_worst_status_wins_and_only_flagged_nodes_are_listed():
    out = node_statuses(clean(kill_switch={"active": True, "reason": "x"}, mode="reduce-only"))
    assert set(out) == {"Kill switch", "Safe mode controller"}


def test_every_flag_label_exists_in_the_v5_svg():
    """Worst case: every rule fires. Each label must be a real box in quant_system_flow_v5.svg."""
    worst = node_statuses(clean(
        kill_switch={"active": True, "reason": "x"}, mode="halt-all",
        loop={"status": "stopped", "last_error": "e"}, market={"verified": False},
        feed={"status": "not connected", "note": "n"},
        unset_limits=["max_order_quantity", "max_adv_pct", "max_daily_loss", "max_open_exposure",
                      "max_symbols_per_portfolio"]))
    root = ET.parse(ROOT / "quant_system_flow_v5.svg").getroot()
    box_labels = {t.text for t in root.iter(SVG_NS + "text") if t.get("font-size") == "15"}
    missing = set(worst) - box_labels
    assert not missing, missing


def test_overview_api_carries_flow_map(tmp_path):
    pytest.importorskip("fastapi")
    testclient = pytest.importorskip("fastapi.testclient")
    sys.path.insert(0, str(ROOT / "dashboard"))
    from app import create_app

    paths = {"ledger": tmp_path / "l.jsonl", "audit": tmp_path / "a.jsonl", "kill": tmp_path / "k.json",
             "limits": tmp_path / "none.yaml", "settings": tmp_path / "none.yaml"}
    body = testclient.TestClient(create_app(paths)).get("/api/overview").json()
    # the feed is never connected yet, so these two are always amber
    assert body["flow"]["Broker feeds"]["status"] == "warn"
    assert body["flow"]["Feed health monitor"]["status"] == "warn"
