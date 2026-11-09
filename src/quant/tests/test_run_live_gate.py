"""run_live startup gate: seven checks, each must block on its own (decision 0030).

Flow stage: Live execution
Status: TEST

startup_checks() is the only thing between a live runner and real money besides
the risk gate. Every test here blocks exactly one gate and proves the runner
refuses with a reason that names the problem. The pass path needs all seven
satisfied, and that is still not live trading: it only means a run MAY start.
"""
import importlib.util
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "src"))

from quant.core.live_approval import APPROVAL_PHRASE, create_approval  # noqa: E402
from quant.core.trading_mode import request_mode  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "run_live", ROOT / "scripts" / "run" / "run_live.py")
run_live = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(run_live)

LIMITS_OK = ("max_order_quantity: 10\nmax_order_notional: 10000\n"
             "max_open_exposure: 20000\nmax_daily_loss: 1000\n"
             "max_strategy_loss: 500\nmax_adv_pct: 0.01\n"
             "max_symbols_per_portfolio: 5\nblocked: false\n")

#: An empty CSV is deliberately UNVERIFIED (calendar.py), so the gate-6 test
#: needs no holiday rows at all.
HOLIDAYS_OK = "date,exchange,name\n2026-01-26,NSE,Republic Day\n"


class _Audit:
    def __init__(self):
        self.rows = []

    def append(self, kind, payload):
        self.rows.append((kind, payload))


def _world(tmp_path, limits_text=LIMITS_OK, holidays=HOLIDAYS_OK):
    """A directory with every gate satisfied. Tests then break ONE thing."""
    limits = tmp_path / "risk_limits.yaml"
    limits.write_text(limits_text, encoding="utf-8")
    holidays_file = tmp_path / "holidays.csv"
    holidays_file.write_text(holidays, encoding="utf-8")
    state = tmp_path / "mode.json"
    approval = tmp_path / "live_approval.json"
    create_approval(approval, approver="Owner", reason="tests",
                    limits_path=limits, phrase=APPROVAL_PHRASE)
    request_mode("live", state_path=state, approval_path=approval, limits_path=limits)
    (tmp_path / "kill_switch.json").write_text(json.dumps({"active": False}))
    return {"limits_path": limits, "mode_file": state, "approval_file": approval,
            "holidays_file": holidays_file}


def _check(world, monkeypatch, audit=None):
    """Run the gate with the API key stubbed (the key is an env concern, not a gate)."""
    monkeypatch.setenv("OPENALGO_API_KEY", "test-key")
    return run_live.startup_checks(
        limits_path=world["limits_path"], mode_file=world["mode_file"],
        approval_file=world["approval_file"], holidays_file=world["holidays_file"],
        audit=audit)


# --------------------------------------------------------------------------- #
# PASS path
# --------------------------------------------------------------------------- #
def test_all_gates_satisfied_allows_a_start(tmp_path, monkeypatch):
    world = _world(tmp_path)
    out = _check(world, monkeypatch)
    assert out["ok"] is True
    assert out["approval"] is not None
    assert out["approval"].approver == "Owner"


def test_the_approval_object_carries_the_reviewed_hash(tmp_path, monkeypatch):
    from quant.core.live_approval import file_sha256
    world = _world(tmp_path)
    out = _check(world, monkeypatch)
    assert out["approval"].limits_sha256 == file_sha256(world["limits_path"])


# --------------------------------------------------------------------------- #
# BLOCK paths - one per gate
# --------------------------------------------------------------------------- #
def test_gate_1_mode_not_live_blocks(tmp_path, monkeypatch):
    world = _world(tmp_path)
    request_mode("paper", state_path=world["mode_file"],
                 approval_path=world["approval_file"], limits_path=world["limits_path"])
    out = _check(world, monkeypatch)
    assert out["ok"] is False
    assert any("not live" in r for r in out["reasons"])


def test_gate_2_and_3_no_approval_blocks(tmp_path, monkeypatch):
    world = _world(tmp_path)
    world["approval_file"].unlink()
    out = _check(world, monkeypatch)
    assert out["ok"] is False
    assert any("live approval" in r for r in out["reasons"])


def test_gate_3_changed_limits_void_the_approval(tmp_path, monkeypatch):
    world = _world(tmp_path)
    world["limits_path"].write_text(LIMITS_OK.replace("10", "99"), encoding="utf-8")
    out = _check(world, monkeypatch)
    assert out["ok"] is False
    assert any("void" in r for r in out["reasons"])


def test_gate_4_blocked_limits_block(tmp_path, monkeypatch):
    world = _world(tmp_path, limits_text=LIMITS_OK.replace("blocked: false", "blocked: true"))
    out = _check(world, monkeypatch)
    assert out["ok"] is False
    assert any("blocked: true" in r for r in out["reasons"])


def test_gate_4_null_limits_block(tmp_path, monkeypatch):
    world = _world(tmp_path, limits_text=LIMITS_OK.replace("max_daily_loss: 1000",
                                                           "max_daily_loss: null"))
    out = _check(world, monkeypatch)
    assert out["ok"] is False
    assert any("incomplete" in r for r in out["reasons"])


def test_gate_5_an_active_kill_switch_blocks(tmp_path, monkeypatch):
    world = _world(tmp_path)
    (tmp_path / "kill_switch.json").write_text(
        json.dumps({"active": True, "reason": "test"}))
    monkeypatch.setattr(run_live, "out_dir_default", lambda: str(tmp_path))
    monkeypatch.setenv("OPENALGO_API_KEY", "test-key")
    out = run_live.startup_checks(limits_path=world["limits_path"],
                                  mode_file=world["mode_file"],
                                  approval_file=world["approval_file"],
                                  holidays_file=world["holidays_file"])
    assert out["ok"] is False
    assert any("kill switch is active" in r for r in out["reasons"])


def test_gate_5_an_unreadable_kill_switch_blocks(tmp_path, monkeypatch):
    """Fail closed: a kill file that cannot be read is not assumed clear."""
    world = _world(tmp_path)
    (tmp_path / "kill_switch.json").write_text("{broken")
    monkeypatch.setattr(run_live, "out_dir_default", lambda: str(tmp_path))
    monkeypatch.setenv("OPENALGO_API_KEY", "test-key")
    out = run_live.startup_checks(limits_path=world["limits_path"],
                                  mode_file=world["mode_file"],
                                  approval_file=world["approval_file"],
                                  holidays_file=world["holidays_file"])
    assert out["ok"] is False
    assert any("unreadable" in r for r in out["reasons"])


def test_gate_6_an_unverified_calendar_blocks(tmp_path, monkeypatch):
    world = _world(tmp_path, holidays="date,exchange\n")   # header only: unverified
    out = _check(world, monkeypatch)
    assert out["ok"] is False
    assert any("not verified" in r for r in out["reasons"])


def test_gate_7_a_missing_api_key_blocks(tmp_path, monkeypatch):
    world = _world(tmp_path)
    monkeypatch.delenv("OPENALGO_API_KEY", raising=False)
    monkeypatch.setenv("OPENALGO_API_KEY", "")
    out = run_live.startup_checks(limits_path=world["limits_path"],
                                  mode_file=world["mode_file"],
                                  approval_file=world["approval_file"],
                                  holidays_file=world["holidays_file"])
    assert out["ok"] is False
    assert any("API key" in r for r in out["reasons"])


def test_a_refusal_is_audited_with_every_reason(tmp_path, monkeypatch):
    world = _world(tmp_path)
    world["approval_file"].unlink()
    audit = _Audit()
    _check(world, monkeypatch, audit=audit)
    assert any(k == "live_start_refused" for k, _ in audit.rows)
    payload = next(p for k, p in audit.rows if k == "live_start_refused")
    assert payload["reasons"], "the audit row must name what blocked the start"


def test_startup_checks_place_no_order_and_touch_no_network():
    """The gate is pure: no order call may appear before main()."""
    src = (ROOT / "scripts" / "run" / "run_live.py").read_text(encoding="utf-8")
    assert ".place(" not in src.split("def main")[0], \
        "startup_checks must not contain an order call"
