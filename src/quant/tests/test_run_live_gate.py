"""run_live startup gate: every gate must block on its own (decision 0030, updated 2026-11-15).

Flow stage: Live execution
Status: TEST

startup_checks() is the only thing between a live runner and real money besides
the risk gate. Every test here blocks exactly one gate and proves the runner
refuses with a reason that names the problem. The pass path needs every gate
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

# Test values only. The real config keeps the book caps, egress IP and capital cap null.
LIMITS_OK = ("max_order_quantity: 10\nmax_order_notional: 10000\n"
             "max_open_exposure: 20000\nmax_daily_loss: 1000\n"
             "max_strategy_loss: 500\nmax_adv_pct: 0.01\n"
             "max_symbols_per_portfolio: 5\nblocked: false\n"
             "max_sector_pct: 0.30\nmax_beta: 1.2\nmax_cvar: 0.05\nmax_drawdown: 0.20\n")
SETTINGS_OK = ("live:\n  allowed_egress_ip: \"203.0.113.10\"\n"
               "  max_capital_inr: 500000\n")
EGRESS_OK = lambda: "203.0.113.10"  # noqa: E731 - injected, so no test touches the network

#: An empty CSV is deliberately UNVERIFIED (calendar.py), so the gate-6 test
#: needs no holiday rows at all.
HOLIDAYS_OK = "date,exchange,name\n2026-01-26,NSE,Republic Day\n"


class _Audit:
    def __init__(self):
        self.rows = []

    def append(self, kind, payload):
        self.rows.append((kind, payload))


def _world(tmp_path, limits_text=LIMITS_OK, holidays=HOLIDAYS_OK, settings_text=SETTINGS_OK):
    """A directory with every gate satisfied. Tests then break ONE thing."""
    limits = tmp_path / "risk_limits.yaml"
    limits.write_text(limits_text, encoding="utf-8")
    holidays_file = tmp_path / "holidays.csv"
    holidays_file.write_text(holidays, encoding="utf-8")
    settings = tmp_path / "settings.yaml"
    settings.write_text(settings_text, encoding="utf-8")
    state = tmp_path / "mode.json"
    approval = tmp_path / "live_approval.json"
    create_approval(approval, approver="Owner", reason="tests",
                    limits_path=limits, phrase=APPROVAL_PHRASE)
    request_mode("live", state_path=state, approval_path=approval, limits_path=limits)
    kill = tmp_path / "kill.json"
    kill.write_text(json.dumps({"active": False}))
    return {"limits_path": limits, "mode_file": state, "approval_file": approval,
            "holidays_file": holidays_file, "settings_path": settings, "kill_path": kill}


def _check(world, monkeypatch, audit=None, egress_fn=EGRESS_OK):
    """Run the gate with the API key stubbed (the key is an env concern, not a gate)."""
    monkeypatch.setenv("OPENALGO_API_KEY", "test-key")
    return run_live.startup_checks(
        limits_path=world["limits_path"], mode_file=world["mode_file"],
        approval_file=world["approval_file"], holidays_file=world["holidays_file"],
        audit=audit, kill_path=world["kill_path"], settings_path=world["settings_path"],
        egress_fn=egress_fn)


# --------------------------------------------------------------------------- #
# PASS path
# --------------------------------------------------------------------------- #
def test_all_gates_satisfied_allows_a_start(tmp_path, monkeypatch):
    world = _world(tmp_path)
    out = _check(world, monkeypatch)
    assert out["ok"] is True
    assert out["approval"] is not None
    assert out["approval"].approver == "Owner"
    assert out["capital_cap"] == 500000.0


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


def test_gate_4_a_null_book_cap_blocks_the_start(tmp_path, monkeypatch):
    """2026-11-15: a null book-level cap switches its check off, so live must not start."""
    for cap in ("max_sector_pct", "max_beta", "max_cvar", "max_drawdown"):
        text = "\n".join(line for line in LIMITS_OK.splitlines() if not line.startswith(cap + ":"))
        (tmp_path / cap).mkdir(exist_ok=True)
        world = _world(tmp_path / cap, limits_text=text + "\n")
        out = _check(world, monkeypatch)
        assert out["ok"] is False, cap
        assert any(cap in r and "book-level" in r for r in out["reasons"]), cap


def test_gate_5_an_active_kill_switch_blocks(tmp_path, monkeypatch):
    world = _world(tmp_path)
    world["kill_path"].write_text(json.dumps({"active": True, "reason": "test"}))
    out = _check(world, monkeypatch)
    assert out["ok"] is False
    assert any("kill switch is active" in r for r in out["reasons"])


def test_gate_5_reads_the_same_kill_file_as_the_live_bot():
    """2026-11-15 fix: the gate once read data/live/kill_switch.json while the bot wrote kill.json."""
    src = (ROOT / "scripts" / "run" / "run_strategy_bot_live.py").read_text(encoding="utf-8")
    assert 'kill_path = state_dir / "kill.json"' in src
    assert "kill_path=kill_path" in src
    assert 'LIVE_BOT_KILL_FILE = ROOT / "data" / "bot_live" / "kill.json"' in (
        ROOT / "scripts" / "run" / "run_live.py").read_text(encoding="utf-8")


def test_gate_5_an_unreadable_kill_switch_blocks(tmp_path, monkeypatch):
    """Fail closed: a kill file that cannot be read is not assumed clear."""
    world = _world(tmp_path)
    world["kill_path"].write_text("{broken")
    out = _check(world, monkeypatch)
    assert out["ok"] is False
    assert any("unreadable" in r for r in out["reasons"])


def test_gate_6_an_unverified_calendar_blocks(tmp_path, monkeypatch):
    world = _world(tmp_path, holidays="date,exchange\n")   # header only: unverified
    out = _check(world, monkeypatch)
    assert out["ok"] is False
    assert any("not verified" in r for r in out["reasons"])


def test_gate_7_a_missing_api_key_blocks(tmp_path, monkeypatch):
    world = _world(tmp_path)
    monkeypatch.setenv("OPENALGO_API_KEY", "")
    out = run_live.startup_checks(limits_path=world["limits_path"],
                                  mode_file=world["mode_file"],
                                  approval_file=world["approval_file"],
                                  holidays_file=world["holidays_file"],
                                  kill_path=world["kill_path"],
                                  settings_path=world["settings_path"], egress_fn=EGRESS_OK)
    assert out["ok"] is False
    assert any("API key" in r for r in out["reasons"])


def test_gate_8_no_whitelisted_ip_setting_blocks(tmp_path, monkeypatch):
    """2026-11-15: the egress check was optional. Now an unset IP refuses the start."""
    world = _world(tmp_path, settings_text="live:\n  allowed_egress_ip: null\n"
                                           "  max_capital_inr: 500000\n")
    out = _check(world, monkeypatch)
    assert out["ok"] is False
    assert any("allowed_egress_ip" in r for r in out["reasons"])


def test_gate_8_an_ip_that_cannot_be_checked_blocks(tmp_path, monkeypatch):
    world = _world(tmp_path)
    out = _check(world, monkeypatch, egress_fn=lambda: None)
    assert out["ok"] is False
    assert any("could not be checked" in r for r in out["reasons"])


def test_gate_8_a_different_ip_blocks(tmp_path, monkeypatch):
    world = _world(tmp_path)
    out = _check(world, monkeypatch, egress_fn=lambda: "198.51.100.7")
    assert out["ok"] is False
    assert any("not the whitelisted" in r for r in out["reasons"])


def test_gate_9_no_capital_cap_blocks(tmp_path, monkeypatch):
    world = _world(tmp_path, settings_text="live:\n  allowed_egress_ip: \"203.0.113.10\"\n"
                                           "  max_capital_inr: null\n")
    out = _check(world, monkeypatch)
    assert out["ok"] is False
    assert any("max_capital_inr" in r for r in out["reasons"])
    assert out["capital_cap"] is None


@__import__("pytest").mark.parametrize("bad", ["0", "-5", "nan", "true"])
def test_gate_9_a_non_positive_or_bad_cap_blocks(tmp_path, monkeypatch, bad):
    world = _world(tmp_path, settings_text=f"live:\n  allowed_egress_ip: \"203.0.113.10\"\n"
                                           f"  max_capital_inr: {bad}\n")
    out = _check(world, monkeypatch)
    assert out["ok"] is False
    assert any("max_capital_inr" in r for r in out["reasons"])


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
