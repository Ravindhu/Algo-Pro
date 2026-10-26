"""
test_data_tools.py - the agent's read-only toolbelt.

Every tool here is called by an LLM, so the invariants are strict:

* no tool can write anything, anywhere;
* no tool can read outside its allowlisted roots;
* a tool error is DATA (``{"error": ...}``), never an exception;
* oversized payloads are refused, not loaded into a prompt.
"""
from __future__ import annotations

import json

import pytest

from blue_engine.core.data_tools import (
    MAX_READ_BYTES,
    DataToolbox,
    build_data_tools,
    summarise_payload,
)


@pytest.fixture()
def roots(tmp_path):
    root = tmp_path / "data"
    (root / "nested").mkdir(parents=True)
    (root / "macro.json").write_text(
        json.dumps({"cpi": {"value": 5.1, "as_of": "2026-10-01"},
                    "rows": [{"close": 1.0}]}), encoding="utf-8")
    (root / "nested" / "deep.json").write_text(json.dumps({"x": 1}),
                                               encoding="utf-8")
    (root / "current_cycle_payload.json").write_text(
        json.dumps({"synced_at": "2026-10-26T09:00:00+00:00",
                    "data_class": "historical_or_derived",
                    "macro": {"cpi": 5.1}}), encoding="utf-8")
    (root / "notes.txt").write_text("not json", encoding="utf-8")
    (tmp_path / "outside.json").write_text(json.dumps({"secret": 1}),
                                           encoding="utf-8")
    return root


@pytest.fixture()
def box(roots):
    return DataToolbox(read_roots=[roots])


# --------------------------------------------------------------------------- #
# list_sources
# --------------------------------------------------------------------------- #
def test_list_sources_lists_json_files_and_providers(box):
    out = box.list_sources({})
    paths = {row["path"] for row in out["json_files"]}
    assert "macro.json" in paths
    assert "nested/deep.json" in paths
    assert "notes.txt" not in paths          # non-JSON never listed
    assert out["providers"] == []


def test_list_sources_names_injected_providers(roots):
    class P:
        def fetch(self):
            return {"cpi": 5.1}

    out = DataToolbox(read_roots=[roots], providers={"economic": P()}).list_sources({})
    assert out["providers"] == ["economic"]


# --------------------------------------------------------------------------- #
# read_json - allowlist
# --------------------------------------------------------------------------- #
def test_read_json_returns_the_payload(box):
    out = box.read_json({"path": "macro.json"})
    assert out["payload"]["cpi"]["value"] == 5.1


def test_read_json_can_return_a_summary_instead_of_every_number(box):
    out = box.read_json({"path": "macro.json", "summary_only": True})
    assert "cpi" in out["payload"]


@pytest.mark.parametrize("path", [
    "../outside.json",                    # traversal out of the root
    "notes.txt",                          # not a .json file
    "missing.json",                       # not present
    "",
])
def test_read_json_refuses_anything_outside_the_allowlist(box, path):
    out = box.read_json({"path": path})
    assert "error" in out


def test_read_json_refuses_an_absolute_path(box, roots):
    out = box.read_json({"path": str((roots / "macro.json").resolve())})
    assert "error" in out
    assert "absolute" in out["error"]


def test_read_json_refuses_an_oversized_payload(tmp_path):
    root = tmp_path / "big"
    root.mkdir()
    (root / "huge.json").write_text(
        json.dumps({"blob": "x" * (MAX_READ_BYTES + 100)}), encoding="utf-8")
    out = DataToolbox(read_roots=[root]).read_json({"path": "huge.json"})
    assert "error" in out
    assert "limit" in out["error"]


def test_read_json_with_no_roots_is_an_error_not_a_crash(tmp_path):
    out = DataToolbox().read_json({"path": "x.json"})
    assert "error" in out


# --------------------------------------------------------------------------- #
# cycle_payload
# --------------------------------------------------------------------------- #
def test_cycle_payload_summarises_the_published_file(box):
    out = box.cycle_payload({})
    assert out["data_class"] == "historical_or_derived"
    assert "macro" in out["shape"]


def test_cycle_payload_missing_is_an_error_with_a_hint(tmp_path):
    out = DataToolbox(read_roots=[tmp_path / "empty"]).cycle_payload({})
    assert "error" in out and "hint" in out


# --------------------------------------------------------------------------- #
# provider
# --------------------------------------------------------------------------- #
def test_provider_reads_a_wired_provider(roots):
    class P:
        def fetch(self):
            return {"policy_rate": {"value": 6.5}}

    out = DataToolbox(read_roots=[roots],
                      providers={"economic": P()}).provider({"name": "economic"})
    assert out["provider"] == "economic"
    assert "policy_rate" in out["shape"]


def test_provider_refuses_an_unwired_name(roots):
    out = DataToolbox(read_roots=[roots]).provider({"name": "nope"})
    assert "error" in out
    assert out["available"] == []


def test_a_provider_that_raises_returns_data_not_an_exception(roots):
    class Boom:
        def fetch(self):
            raise RuntimeError("feed down")

    out = DataToolbox(read_roots=[roots],
                      providers={"economic": Boom()}).provider({"name": "economic"})
    assert "error" in out and "feed down" in out["error"]


# --------------------------------------------------------------------------- #
# price_history / data_catalog
# --------------------------------------------------------------------------- #
def test_price_history_without_a_symbol_is_an_error(box):
    assert "error" in box.price_history({})


def test_price_history_never_raises_on_a_missing_cache(box):
    out = box.price_history({"symbol": "INFY", "limit": 5})
    assert isinstance(out, dict)
    assert "rows" in out or "error" in out


def test_data_catalog_reports_existence(box, tmp_path):
    existing = tmp_path / "data" / "macro.json"
    out = box.data_catalog({}) if not box.catalog_paths else box.data_catalog({})
    assert out["count"] == 0
    box.catalog_paths = [existing, tmp_path / "nope.json"]
    out = box.data_catalog({})
    assert [row["exists"] for row in out["artifacts"]] == [True, False]


# --------------------------------------------------------------------------- #
# Construction + invariants
# --------------------------------------------------------------------------- #
def test_every_tool_is_callable_and_returns_a_dict(box):
    for name, tool in box.tools().items():
        out = tool({})
        assert isinstance(out, dict), name


def test_the_toolbelt_has_no_write_capability(box):
    out = box.describe()
    assert out["writable"] is False
    assert "write" not in " ".join(out["tools"]).lower()


def test_build_data_tools_returns_the_same_tool_set(roots):
    tools = build_data_tools(read_roots=[roots])
    assert set(tools) == {"list_sources", "read_json", "cycle_payload",
                          "price_history", "provider", "data_catalog"}


def test_summarise_payload_bounds_the_size_of_a_large_structure():
    payload = {f"k{i}": {"deep": {"deeper": [1, 2, 3]}} for i in range(500)}
    summary = summarise_payload(payload, depth=1)
    assert len(summary) <= 200
