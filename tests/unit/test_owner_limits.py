"""Owner limits (config/risk_limits.yaml) are merged into the blue engine's risk block.

The blue runner reads master.json, which carries looser caps. The merge must keep the
stricter value and must never loosen an owner cap.
"""
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src" / "quant"))  # the blue_engine package lives here
sys.path.insert(0, str(ROOT / "src"))

from blue_engine.core.owner_limits import apply_owner_limits  # noqa: E402

LOOSE_MASTER = {"risk": {
    "require_live_ack": True, "blocked": False, "allowed_actions": ["BUY", "SELL"],
    "max_order_quantity": 500, "max_order_notional": 250000.0,
    "max_daily_loss": 10000.0, "max_open_exposure": 500000.0,
}}


def _owner(tmp_path, body):
    p = tmp_path / "risk_limits.yaml"
    p.write_text(body, encoding="utf-8")
    return p


def test_stricter_owner_value_wins_over_loose_master(tmp_path):
    owner = _owner(tmp_path, "max_order_quantity: 10\nmax_order_notional: 10000\n"
                             "max_daily_loss: 1000\nmax_open_exposure: 20000\n")
    risk = apply_owner_limits(LOOSE_MASTER, owner)["risk"]
    assert risk["max_order_quantity"] == 10
    assert risk["max_order_notional"] == 10000
    assert risk["max_daily_loss"] == 1000
    assert risk["max_open_exposure"] == 20000


def test_owner_never_loosens_a_tighter_master(tmp_path):
    owner = _owner(tmp_path, "max_order_quantity: 1000\n")
    cfg = {"risk": {"max_order_quantity": 5}}
    assert apply_owner_limits(cfg, owner)["risk"]["max_order_quantity"] == 5


def test_owner_cap_is_added_when_master_has_none(tmp_path):
    owner = _owner(tmp_path, "max_open_exposure: 20000\n")
    risk = apply_owner_limits({"risk": {}}, owner)["risk"]
    assert risk["max_open_exposure"] == 20000


def test_null_owner_cap_adds_nothing(tmp_path):
    owner = _owner(tmp_path, "max_order_quantity: null\n")
    risk = apply_owner_limits({"risk": {}}, owner)["risk"]
    assert "max_order_quantity" not in risk


def test_missing_owner_file_blocks_entries(tmp_path):
    risk = apply_owner_limits(LOOSE_MASTER, tmp_path / "does_not_exist.yaml")["risk"]
    assert risk["blocked"] is True


def test_owner_blocked_flag_propagates(tmp_path):
    owner = _owner(tmp_path, "blocked: true\n")
    assert apply_owner_limits(LOOSE_MASTER, owner)["risk"]["blocked"] is True


def test_input_config_is_not_mutated(tmp_path):
    owner = _owner(tmp_path, "max_order_quantity: 10\n")
    apply_owner_limits(LOOSE_MASTER, owner)
    assert LOOSE_MASTER["risk"]["max_order_quantity"] == 500


def test_repo_owner_file_loads_and_tightens_the_real_master():
    """Against the committed owner file and the committed master.json."""
    import json
    master = json.loads((ROOT / "src/quant/blue_engine/input/master.json").read_text(encoding="utf-8"))
    risk = apply_owner_limits(master)["risk"]
    assert risk["max_order_quantity"] == 10
    assert risk["max_order_notional"] == 10000
    assert risk["max_daily_loss"] == 1000
    assert risk["max_open_exposure"] == 20000
