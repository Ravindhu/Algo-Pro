"""
Tests for the neural-shell topology (dashboard/web/flow.py).

Checks the graph is internally consistent and that node status follows the
health of the page that publishes each engine's data.
"""
from __future__ import annotations

import json

import pytest

from blue_engine.dashboard.web import flow, sections

VALID_STATUS = {"ok", "warn", "bad", "idle", "external"}


@pytest.fixture
def fixed_payloads(monkeypatch):
    """Replace section reads with fixed payloads (healthy unless overridden)."""
    overrides: dict = {}

    def fake(section):
        status = overrides.get(section, "ok")
        return {"key": section, "title": section, "status": status,
                "summary": [{"label": "x", "value": "1"}], "tables": [],
                "notes": [], "error": None}

    monkeypatch.setattr(flow, "_payload", fake)
    return overrides


# ------------------------------------------------------------- structure
def test_every_main_engine_reference_exists():
    for _nid, _label, _col, _row, _kind, engine in flow.MAIN_NODES:
        if engine is not None:
            assert engine in flow.ENGINES, engine


def test_every_main_edge_endpoint_is_a_node():
    ids = {n[0] for n in flow.MAIN_NODES}
    for a, b, kind in flow.MAIN_EDGES:
        assert a in ids and b in ids, (a, b)
        assert kind in {"flow", "feedback"}


def test_main_node_ids_are_unique():
    ids = [n[0] for n in flow.MAIN_NODES]
    assert len(ids) == len(set(ids))


def test_every_engine_has_its_own_shell_graph():
    assert set(flow.ENGINES) == set(flow.ENGINE_GRAPH)
    assert len(flow.ENGINES) == 14


def test_engine_graph_edges_reference_its_nodes():
    for key, (nodes, edges) in flow.ENGINE_GRAPH.items():
        ids = {n[0] for n in nodes}
        assert len(ids) == len(nodes), key
        for a, b, kind in edges:
            assert a in ids and b in ids, (key, a, b)
            assert kind in {"flow", "feedback"}, key


def test_node_kinds_columns_and_rows_are_valid():
    for key, (nodes, _edges) in flow.ENGINE_GRAPH.items():
        for _i, _label, col, row, kind in nodes:
            assert kind in flow._KINDS, (key, kind)
            assert isinstance(col, int) and col >= 0, key
            assert 0.0 <= row <= 1.0, key
    for _nid, _label, col, row, kind, _engine in flow.MAIN_NODES:
        assert 0 <= col < flow.MAIN_COLS
        assert 0.0 <= row <= 1.0 and kind in flow._KINDS


def test_engine_sections_are_real_or_none():
    known = {k for k, _ in sections.SECTIONS}
    for key, (_title, _group, _blurb, section) in flow.ENGINES.items():
        assert section is None or section in known, key


# ------------------------------------------------------------- status
def test_healthy_pages_make_every_engine_ok_except_unwired(fixed_payloads):
    data = flow.build_flow()
    by_key = {e["key"]: e["status"] for e in data["engines"]}
    assert by_key["exit"] == "idle"                       # no page wired
    assert all(s == "ok" for k, s in by_key.items() if k != "exit")


def test_error_and_empty_pages_map_to_bad_and_warn(fixed_payloads):
    fixed_payloads.update({"economic": "error", "sectors": "empty"})
    by_key = {e["key"]: e["status"] for e in flow.build_flow()["engines"]}
    assert by_key["economic"] == "bad"
    assert by_key["sector"] == "warn"


def test_main_node_takes_its_engine_status(fixed_payloads):
    fixed_payloads["risk"] = "error"
    nodes = {n["id"]: n for n in flow.build_flow()["nodes"]}
    assert nodes["gate"]["status"] == "bad"
    assert nodes["kill"]["status"] == "bad"


def test_external_nodes_are_never_marked_healthy(fixed_payloads):
    for node in flow.build_flow()["nodes"]:
        if node["kind"] == "external":
            assert node["status"] == "external"


def test_counts_add_up_to_the_engine_total(fixed_payloads):
    fixed_payloads["ml"] = "empty"
    data = flow.build_flow()
    assert sum(data["counts"].values()) == len(flow.ENGINES)
    assert data["counts"]["warn"] == 1 and data["counts"]["idle"] == 1


def test_all_statuses_are_in_the_allowed_set(fixed_payloads):
    fixed_payloads.update({"risk": "error", "ml": "empty"})
    data = flow.build_flow()
    for node in data["nodes"]:
        assert node["status"] in VALID_STATUS
    for key in flow.ENGINES:
        assert flow.build_engine(key)["status"] in VALID_STATUS


# ------------------------------------------------------------- pulses
def test_pulse_runs_only_between_live_ends():
    assert flow._active("ok", "ok")
    assert flow._active("external", "ok")
    assert not flow._active("external", "external")   # broker -> exchange
    assert not flow._active("ok", "bad")
    assert not flow._active("ok", "idle")


def test_healthy_flow_shows_pulses_and_failed_flow_does_not(fixed_payloads):
    healthy = [e["active"] for e in flow.build_flow()["edges"]]
    assert any(healthy)
    fixed_payloads["risk"] = "error"
    failed = flow.build_flow()["edges"]
    gate_edges = [e for e in failed if "gate" in (e["from"], e["to"])]
    assert gate_edges and not any(e["active"] for e in gate_edges)


# ------------------------------------------------------------- engines
def test_build_engine_returns_shell_and_payload(fixed_payloads):
    data = flow.build_engine("market")
    assert data["title"] == "Market data"
    assert data["section"] == "terminal"
    assert data["payload"]["status"] == "ok"
    assert len(data["nodes"]) == len(flow.ENGINE_GRAPH["market"][0])
    assert data["cols"] == 4


def test_engine_without_a_page_has_no_payload(fixed_payloads):
    data = flow.build_engine("exit")
    assert data["status"] == "idle"
    assert data["payload"] is None


def test_unknown_engine_returns_none():
    assert flow.build_engine("nope") is None


def test_engine_inputs_are_external_and_do_not_pulse_on_their_own(fixed_payloads):
    data = flow.build_engine("slicer")
    srcs = [n for n in data["nodes"] if n["kind"] == "src"]
    assert srcs and all(n["status"] == "external" for n in srcs)


# ------------------------------------------------------------- safety
def test_flow_payloads_are_json_and_leak_no_secret_keys(fixed_payloads):
    blob = json.dumps({"main": flow.build_flow(),
                       "engines": {k: flow.build_engine(k) for k in flow.ENGINES}}, default=str)
    lowered = blob.lower()
    for marker in ("api_key", "password", "pepper", "secret"):
        assert marker not in lowered, marker


def test_flow_module_has_no_execution_imports():
    import ast
    from pathlib import Path
    source = Path(flow.__file__).read_text(encoding="utf-8")
    banned = ("blue_engine.core.client", "blue_engine.pov", "blue_engine.core.runner",
              "openalgo", "placeorder")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        names = []
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            names = [node.module]
        for name in names:
            assert not any(name.startswith(b) for b in banned), name
