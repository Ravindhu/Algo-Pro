"""Recommendation store + flow_status recommendation fields (Part B 1-2)."""
from __future__ import annotations

import pytest

from quant.core import recommendations as recs


def test_put_get_and_roundtrip(tmp_path):
    path = tmp_path / "recs.json"
    entry = recs.put("Loss budget breaker", "max_daily_loss", None, 2500.0,
                     "agent suggests a loss budget", path=path, now=1000.0)
    got = recs.get(entry["id"], path=path, now=1005.0)
    assert got["recommended"] == 2500.0 and got["station"] == "Loss budget breaker"


def test_expiry_refuses_and_drops(tmp_path):
    path = tmp_path / "recs.json"
    entry = recs.put("s", "max_daily_loss", None, 1, "r", path=path, now=1000.0, ttl_s=60)
    assert recs.get(entry["id"], path=path, now=2000.0) is None
    recs.put("s", "max_daily_loss", None, 1, "r", path=path, now=1000.0, ttl_s=60)
    fresh = recs.put("s", "max_order_notional", None, 2, "r", path=path, now=2000.0, ttl_s=60)
    live_ids = [e["id"] for e in recs.live(path=path, now=2005.0)]
    assert entry["id"] not in live_ids and fresh["id"] in live_ids


def test_remove_makes_apply_one_use(tmp_path):
    path = tmp_path / "recs.json"
    entry = recs.put("s", "p", 1, 2, "r", path=path, now=1000.0)
    assert recs.remove(entry["id"], path) is True
    assert recs.remove(entry["id"], path) is False
    assert recs.get(entry["id"], path=path, now=1001.0) is None


def test_unreadable_file_means_no_recommendations(tmp_path):
    path = tmp_path / "broken.json"
    path.write_text("{not json", encoding="utf-8")
    assert recs.live(path=path) == []
    assert recs.get("whatever", path=path) is None


# --- flow_status wiring (Part B step 2) --------------------------------------
def test_node_status_carries_recommendation_fields():
    from quant.reporting.flow_status import node_statuses

    overview = {"unset_limits": ["max_daily_loss"]}
    recs_in = [{"id": "abc123", "station": "agent", "parameter": "max_daily_loss",
                "current": None, "recommended": 2500.0, "reason": "loss budget"}]
    board = node_statuses(overview, recs_in)
    entry = board["Loss budget breaker"]
    assert entry["status"] == "warn"
    assert entry["recommendation"] == "abc123"
    assert entry["recommended_value"] == 2500.0
    assert entry["can_apply"] is True


def test_node_status_without_recommendations_is_unchanged():
    from quant.reporting.flow_status import node_statuses

    board = node_statuses({"unset_limits": ["max_daily_loss"]})
    entry = board["Loss budget breaker"]
    assert "can_apply" not in entry and "recommendation" not in entry

