"""Replay event record for the risk ladder (plan P3 prerequisite). Checks the shape, not the market logic."""
from quant.strategies.cycle import CycleResult, Entry, _finish


def test_entry_carries_its_ocl_id_and_defaults_to_none():
    assert Entry(1, 100.0, "aggressive").ocl is None
    assert Entry(1, 100.0, "aggressive", "OCL-A").ocl == "OCL-A"


def test_finish_passes_ordered_events_through():
    events = [("aggressive", "OCL-A", 100.0, 1), ("invalidated", "OCL-A", 95.0, 2)]
    res = _finish("egpattern_open", "waiting", 93.0, [], False, events, None, 1_000_000.0, 0.01)
    assert isinstance(res, CycleResult)
    assert res.events == events


def test_finish_without_events_gives_empty_list():
    res = _finish("egpattern_open", "waiting", 93.0, [], False, [], None, 1_000_000.0, 0.01)
    assert res.events == []
