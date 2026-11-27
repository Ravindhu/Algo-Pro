"""Phase 3 live blockers (2026-11-27), part 1: one kill check, screen clock, symbols, backups.

Covers audit items 2 and 13 (kill switches), 1 (screen clock), 12 (symbol regex), 17 (pre-restore
retention) and 9 (paper runner uses the owner's M and A switches).
"""
import importlib.util
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from quant.core import engine_control as eng
from quant.data.manual_symbols import SYMBOL_RE
from quant.data.screen_refresh import source_is_stale
from quant.risk.kill_switch import operator_kill_active

ROOT = Path(__file__).resolve().parents[2]
IST = timezone(timedelta(hours=5, minutes=30))


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _kill(root: Path, rel: str, active: bool, reason: str = "test"):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"active": active, "reason": reason}), encoding="utf-8")


# ---- items 2, 13: one kill check -----------------------------------------------------------

def test_no_kill_files_means_clear(tmp_path):
    assert operator_kill_active(tmp_path) == (False, "")


@pytest.mark.parametrize("rel,name", [
    ("data/paper/kill_switch.json", "paper"),
    ("data/bot/kill.json", "paper_bot"),
    ("data/bot_live/kill.json", "live_bot"),
])
def test_any_one_active_kill_file_blocks(tmp_path, rel, name):
    _kill(tmp_path, rel, True, "owner stop")
    active, reason = operator_kill_active(tmp_path)
    assert active and name in reason and "owner stop" in reason


def test_unreadable_kill_file_counts_as_on(tmp_path):
    path = tmp_path / "data" / "bot_live" / "kill.json"
    path.parent.mkdir(parents=True)
    path.write_text("{not json", encoding="utf-8")
    assert operator_kill_active(tmp_path)[0] is True


def test_start_is_refused_when_only_the_live_kill_is_on(tmp_path, monkeypatch):
    # Before Phase 3 a start checked only the paper kill file, so the live kill never stopped it.
    monkeypatch.setattr(eng, "ROOT", tmp_path)
    _kill(tmp_path, "data/bot_live/kill.json", True, "live stop")
    launched = []
    monkeypatch.setattr(eng.subprocess, "Popen", lambda *a, **k: launched.append(a) or None)
    ok, message, pid = eng.start_engine(kill_active=False, pid_path=tmp_path / "engine.pid",
                                        log_path=tmp_path / "m.log")
    assert ok is False and pid is None and "live_bot" in message
    assert launched == []


def test_start_is_allowed_when_no_kill_is_on(tmp_path, monkeypatch):
    monkeypatch.setattr(eng, "ROOT", tmp_path)
    class _P:
        pid = 6161
    monkeypatch.setattr(eng.subprocess, "Popen", lambda *a, **k: _P())
    monkeypatch.setattr(eng, "running_pid", lambda *a, **k: None)
    ok, _, pid = eng.start_engine(kill_active=False, pid_path=tmp_path / "engine.pid",
                                  log_path=tmp_path / "m.log")
    assert ok and pid == 6161


def test_preflight_kill_gate_blocks_on_owner_kill(tmp_path, monkeypatch):
    import sys
    import types
    from collections import namedtuple
    # The supervisor module imports the OpenAlgo SDK, which this sandbox lacks. Stub only what the
    # gate uses; the real module is imported by build_gates on the trading PC.
    GateResult = namedtuple("GateResult", "name ok reason")
    stub = types.ModuleType("quant.engines.core.supervisor")
    stub.GateResult = GateResult
    stub.kill_switch_gate = lambda: GateResult("kill_switch", True, "clear")
    monkeypatch.setitem(sys.modules, "quant.engines.core.supervisor", stub)
    sb = _load("supervise_bot_p3", "scripts/ops/supervise_bot.py")
    monkeypatch.setattr(sb, "ROOT", tmp_path)
    _kill(tmp_path, "data/paper/kill_switch.json", True, "dashboard stop")
    gate = sb.owner_kill_gate()
    assert gate.ok is False and "paper" in gate.reason
    # And with no owner kill on, the gate defers to the supervisor's own check.
    (tmp_path / "data/paper/kill_switch.json").unlink()
    assert sb.owner_kill_gate().ok is True


def test_preflight_kill_gate_is_wired_into_the_gate_list():
    src = (ROOT / "scripts/ops/supervise_bot.py").read_text(encoding="utf-8")
    assert "owner_kill_gate," in src
    assert "        kill_switch_gate," not in src


# ---- item 1: screen clock -------------------------------------------------------------------

def _screen(tmp_path, as_of, mtime_age_s=0.0):
    import os
    path = tmp_path / "last_screen.json"
    path.write_text(json.dumps({"as_of": as_of, "results": []}), encoding="utf-8")
    if mtime_age_s:
        t = datetime.now().timestamp() - mtime_age_s
        os.utime(path, (t, t))
    return path


def _ist_now():
    return datetime.now(IST).replace(tzinfo=None)


def test_stale_by_as_of_even_when_the_file_was_just_copied(tmp_path):
    old = (_ist_now() - timedelta(hours=2)).isoformat()
    path = _screen(tmp_path, old, mtime_age_s=0)            # mtime says "just written"
    assert source_is_stale(path, datetime.now(timezone.utc), every_minutes=60) is True


def test_fresh_by_as_of_even_when_the_mtime_is_old(tmp_path):
    fresh = (_ist_now() - timedelta(minutes=5)).isoformat()
    path = _screen(tmp_path, fresh, mtime_age_s=7 * 24 * 3600)   # mtime says "a week old"
    assert source_is_stale(path, datetime.now(timezone.utc), every_minutes=60) is False


def test_future_as_of_is_not_fresh(tmp_path):
    future = (_ist_now() + timedelta(hours=3)).isoformat()
    path = _screen(tmp_path, future)
    assert source_is_stale(path, datetime.now(timezone.utc), every_minutes=60) is True


# ---- item 12: symbol validator ---------------------------------------------------------------

@pytest.mark.parametrize("good", ["INFY", "M&M", "BAJAJ-AUTO", "500325", "A1"])
def test_real_symbols_pass(good):
    assert SYMBOL_RE.match(good)


@pytest.mark.parametrize("junk", ["-----", "&&&&", "0", "", "infy", "12345", "1234567", "A B"])
def test_junk_symbols_are_refused(junk):
    assert not SYMBOL_RE.match(junk)


# ---- item 9: paper runner uses the same owner switches as live -------------------------------

def test_paper_runner_passes_the_owner_switches():
    src = (ROOT / "scripts/run/run_strategy_bot.py").read_text(encoding="utf-8")
    assert "manual_symbols.json" in src and "source_mode.json" in src


# ---- item 17: pre-restore retention ----------------------------------------------------------

def test_pre_restore_folders_are_pruned_to_the_newest_keep(tmp_path):
    bk = _load("backup_state_p3", "scripts/ops/backup_state.py")
    names = [f"pre-restore-2026112{d}-10000{d}" for d in range(5)]
    for n in names:
        (tmp_path / n).mkdir()
    (tmp_path / "20261127-100000").mkdir()          # an ordinary backup: never touched here
    removed = bk.prune_pre_restore(tmp_path, keep=2)
    assert sorted(removed) == sorted(names[:3])
    assert sorted(p.name for p in tmp_path.iterdir()) == sorted(names[3:] + ["20261127-100000"])


def test_pre_restore_prune_with_keep_zero_deletes_nothing(tmp_path):
    bk = _load("backup_state_p3b", "scripts/ops/backup_state.py")
    (tmp_path / "pre-restore-20261127-100000").mkdir()
    assert bk.prune_pre_restore(tmp_path, keep=0) == []
    assert (tmp_path / "pre-restore-20261127-100000").exists()
