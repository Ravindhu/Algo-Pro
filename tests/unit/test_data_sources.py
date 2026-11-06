"""Data-source rules: INFY is test-only; any live source needs a licence note (decision 0017).

Flow stage: Data and features
Status: PHASE 1 IMPLEMENTED
"""
import pathlib

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[2]


def _sources():
    data = yaml.safe_load((ROOT / "config" / "data_sources.yaml").read_text(encoding="utf-8"))
    return data["sources"]


def test_infy_is_test_only_and_not_live():
    infy = _sources()["infy_sample"]
    assert infy["purpose"] == "test_only"
    assert infy["live_allowed"] is False


def test_any_live_source_has_a_licence_note_and_is_not_infy():
    for name, src in _sources().items():
        if src["live_allowed"]:
            assert src["licence"], name
            assert name != "infy_sample", name
