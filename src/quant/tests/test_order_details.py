"""Execution log details: planner slot numbers, the per-order record, and the join with fills (owner 2026-11-25).

Flow stage: Execution (order record), Risk (planner)
Status: PHASE 3 IMPLEMENTED
"""
import json
import sys
import pathlib
from datetime import datetime

sys.path.insert(0, str(pathlib.Path(__file__).parent))

from quant.engines.alpha.strategies.risk_tree import plan  # noqa: E402
from quant.engines.alpha.strategies.risk_ladder import Order, RiskLadder  # noqa: E402
from quant.execution.order_details import OrderDetailStore, build_details, level_text  # noqa: E402
from quant.reporting.portfolio_view import HoldingsReader, fill_log  # noqa: E402


def _decision(events, kind, ocl=None):
    return [d for d in plan(events, 4.0) if d.event[0] == kind and (ocl is None or d.event[1] == ocl)]


def test_mtf_and_htf_aggressive_slots_are_numbered_and_the_leaf_carries_both():
    events = [("htf_ocl", "H1"), ("ltf_ocl", "L1"), ("ltf_agg_touch", "L1")]
    mtf = _decision(events, "htf_ocl", "H1")[0]
    assert (mtf.level, mtf.stage, mtf.slot) == ("mtf", "aggressive", 1)
    leaf = _decision(events, "ltf_agg_touch", "L1")[0]
    assert leaf.stage == "leaf_aggressive"
    assert leaf.context == (("aggressive", 1), ("aggressive", 1), "leaf_aggressive")


def test_third_aggressive_then_conservative_slot_one_after_the_cons_touch():
    events = [("htf_ocl", "H1"), ("htf_ocl", "H2"), ("htf_ocl", "H3"), ("mtf_cons_touch",), ("htf_ocl", "H4")]
    slots = [(d.stage, d.slot) for d in plan(events, 4.0) if d.event[0] == "htf_ocl"]
    assert slots == [("aggressive", 1), ("aggressive", 2), ("aggressive", 3), ("conservative", 1)]


def test_refused_slots_carry_no_number():
    events = [("htf_ocl", "H1"), ("htf_ocl", "H2"), ("htf_ocl", "H3"), ("htf_ocl", "H4")]
    refused = _decision(events, "htf_ocl", "H4")[0]
    assert refused.refused is not None and refused.slot is None


def test_level_text_and_details_shape():
    assert level_text("aggressive", 2) == "agg 2"
    assert level_text("conservative", 1) == "con 1"
    assert level_text("leaf_conservative", None) == "con"
    assert level_text(None, None) == "—"
    d = build_details("ocl_keylevel", 0.79123, (("aggressive", 2), ("conservative", 1), "leaf_aggressive"))
    assert d == {"strategy": "OCL-OCL", "risk_pct": 0.791, "mtf": "agg 2", "htf": "con 1", "ltf": "agg"}
    assert build_details("engulfing_fail", 0.5, ())["strategy"] == "EGF-engulfing fail"
    assert build_details("unknown_strategy", 0.5, ())["strategy"] == "unknown_strategy"


def test_store_round_trip_survives_reopen_and_missing_or_bad_file(tmp_path):
    path = tmp_path / "order_details.json"
    OrderDetailStore(path).put("ORD1", {"strategy": "EG-engulfing", "risk_pct": 0.5, "qty": 3})
    assert json.loads(path.read_text(encoding="utf-8"))["ORD1"]["qty"] == 3
    assert OrderDetailStore(path).get("ORD1")["strategy"] == "EG-engulfing"
    assert OrderDetailStore(path).get("NOPE") is None
    path.write_text("not json", encoding="utf-8")
    assert OrderDetailStore(path).get("ORD1") is None  # unreadable reads as empty, never a guess


def test_store_keeps_the_newest_records_only(tmp_path, monkeypatch):
    import quant.execution.order_details as od
    monkeypatch.setattr(od, "_MAX_RECORDS", 2)
    store = OrderDetailStore(tmp_path / "d.json")
    for oid in ("A", "B", "C"):
        store.put(oid, {"strategy": oid})
    assert store.get("A") is None and store.get("C")["strategy"] == "C"


def test_ladder_order_keeps_details_through_save_and_load():
    ladder = RiskLadder(equity=1_000_000.0, htf_low=95.0)
    order = Order(ocl_id="H1", price=100.0, qty=10, risk=0.01, reason="aggressive",
                  details={"strategy": "EG-engulfing", "risk_pct": 0.8})
    back = Order.from_doc(order.to_doc())
    assert back.details == {"strategy": "EG-engulfing", "risk_pct": 0.8}
    assert Order.from_doc({"ocl_id": "H1", "price": 1, "qty": 1, "risk": 0.1, "reason": "x", "filled": 0}).details == {}
    assert ladder is not None


def test_fill_log_carries_order_id_and_reader_attaches_our_record():
    trades = [{"symbol": "INFY", "exchange": "NSE", "action": "BUY", "quantity": 5, "product": "MTF",
               "orderid": "ORD9", "averageprice": 100.0, "timestamp": "2026-11-25 10:00:00"}]
    rows = fill_log(trades)
    assert rows[0]["orderid"] == "ORD9"

    def fetch(key):
        return {"status": "success", "data": {"holdings": [], "statistics": {}}}

    def trades_fn(key):
        return {"status": "success", "data": trades}

    lookup = {"ORD9": {"strategy": "FOCL-T sell fail ocl top", "risk_pct": 0.2}}
    reader = HoldingsReader(fetch, lambda: "k", lambda: 1_800_000_000.0, trades=trades_fn,
                            details=lambda oid: lookup.get(oid))
    fills = reader.read()["fills"]
    assert fills and fills[0]["details"]["strategy"] == "FOCL-T sell fail ocl top"
    assert fills[0].get("details") is not None


def test_child_fill_saves_the_active_orders_details(tmp_path):
    from test_margin_bot import _bot
    from quant.execution.strategy_bot import _Parent
    from quant.core.types import TradeIntent
    bot = _bot(tmp_path)
    store = OrderDetailStore(tmp_path / "details.json")
    bot._order_details = store

    class _Ladder:
        def record_fill(self, order, qty):
            order.filled += qty

    intent = TradeIntent(intent_id="INFY-IN", strategy_id="ocl_keylevel", symbol="INFY", side="BUY", qty=10,
                         ref_price=100.0, kind="entry", prev_close=None, trade_day=datetime(2026, 11, 25).date(),
                         product="CNC")
    parent = _Parent(key="INFY|s|cycle|1", intent=intent, remaining=10, circuit_pct=20.0, cycle=True,
                     ladder=_Ladder(), stop=95.0)
    active = Order(ocl_id="H1", price=100.0, qty=10, risk=0.01, reason="aggressive",
                   details={"strategy": "OCL-OCL", "risk_pct": 0.79, "mtf": "agg 1", "htf": "—", "ltf": "agg"})
    report = {"children_sent": 0}
    bot._book_child(parent, parent.key, active, 10, 100.0, {"status": "success", "orderid": "BROKER-7"}, report,
                    sent_qty=8, pov_qty=12)
    saved = store.get("BROKER-7")
    assert saved["strategy"] == "OCL-OCL" and saved["qty"] == 10 and saved["ltf"] == "agg"
    assert (saved["total_qty"], saved["pov_qty"], saved["slicer_qty"]) == (10, 12, 8)
