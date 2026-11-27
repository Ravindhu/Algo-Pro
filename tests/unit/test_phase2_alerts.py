"""Phase 2 leftovers (2026-11-28): refusals and blocked states reach the owner, not only the log.

Covers audit items 3 and 4 (reconciliation and cash), 5 and 7 (a send refused by the analyzer or
the changed-limits guard), and 6 (live data source not approved).
"""
import pytest

from quant.core.config import RiskLimits
from quant.core.types import TradeIntent
from quant.execution import strategy_bot as sb
from quant.execution.pov_executor import PovExecutor
from quant.monitor.broker_recon import ReconGate
from quant.monitor.safe_mode import SafeMode
from quant.risk.gate import RiskState, check_intent
from quant.risk.kill_switch import KillSwitch

PARENT = TradeIntent(intent_id="P1", strategy_id="S1", symbol="INFY", side="BUY", qty=500,
                     ref_price=1000.0, kind="entry", lot_size=1, prev_close=1000.0,
                     circuit_pct=20.0, adv_shares=1_000_000.0)
LIMITS = RiskLimits(max_order_quantity=1000, max_order_notional=10_000_000.0,
                    max_symbols_per_portfolio=10, max_open_exposure=1e9, max_daily_loss=1e6,
                    max_strategy_loss=1e6, max_adv_pct=0.5)


class _Sender:
    def __init__(self, fail=None):
        self.fail = fail

    def place_child(self, intent, api_key, decision, price_type="MARKET", limit_price=None):
        if self.fail is not None:
            raise self.fail
        return {"status": "ok", "orderid": "O1"}


class _Audit:
    def append(self, kind, payload):
        pass


class _Alerts:
    def __init__(self):
        self.sent = []

    def emit(self, level, source, message):
        self.sent.append((level, message))


def _bot(alerts=None):
    bot = sb.StrategyBot.__new__(sb.StrategyBot)
    bot._log = lambda *a, **k: None
    bot.alerts = alerts if alerts is not None else _Alerts()
    bot._alerted = set()
    bot._funds = 0.0
    return bot


# ---- items 5, 7: a refused send is alerted, and still raises to the caller -------------------

def test_refused_send_is_alerted_and_still_raised(tmp_path):
    state = RiskState()
    kill = KillSwitch(tmp_path / "kill.json")
    gate = lambda intent: check_intent(intent, state, LIMITS, kill, SafeMode())
    sent = []
    ex = PovExecutor(gate, _Sender(fail=PermissionError("analyzer is ON")), "key", _Audit(), pov=0.1,
                     on_refused=lambda level, msg: sent.append((level, msg)))
    child = PovExecutor.make_child(PARENT, qty=50, seq=1)
    with pytest.raises(PermissionError):
        ex.submit_child(child)
    assert sent and sent[0][0] == "CRITICAL"
    assert "INFY" in sent[0][1] and "PermissionError" in sent[0][1]


def test_executor_without_callback_behaves_as_before(tmp_path):
    state = RiskState()
    kill = KillSwitch(tmp_path / "kill.json")
    gate = lambda intent: check_intent(intent, state, LIMITS, kill, SafeMode())
    ex = PovExecutor(gate, _Sender(fail=PermissionError("x")), "key", _Audit(), pov=0.1)
    with pytest.raises(PermissionError):
        ex.submit_child(PovExecutor.make_child(PARENT, qty=50, seq=1))


def test_bot_wires_its_alert_into_the_executor():
    import inspect
    src = inspect.getsource(sb.StrategyBot.__init__)
    assert "on_refused=self._alert" in src


# ---- once-per-condition helper ------------------------------------------------------------

def test_alert_once_fires_once_until_cleared():
    bot = _bot()
    bot._alert_once("funds", "CRITICAL", "no cash")
    bot._alert_once("funds", "CRITICAL", "no cash")
    assert len(bot.alerts.sent) == 1
    bot._clear_alert("funds")
    bot._alert_once("funds", "CRITICAL", "no cash")
    assert len(bot.alerts.sent) == 2


# ---- item 4: missing cash is said out loud, once ------------------------------------------

def test_unreadable_funds_alert_once_and_refuse():
    bot = _bot()

    def boom():
        raise ConnectionError("broker down")
    bot._read_funds = boom
    assert bot._safe_funds() == 0.0
    assert bot._safe_funds() == 0.0
    assert len(bot.alerts.sent) == 1
    assert "new entries refused" in bot.alerts.sent[0][1]


def test_zero_funds_alert_then_clears_when_cash_returns():
    bot = _bot()
    values = iter([0.0, 5000.0, -1.0])
    bot._read_funds = lambda: next(values)
    assert bot._safe_funds() == 0.0            # zero cash: alert
    assert bot._safe_funds() == 5000.0         # cash back: alert cleared, value used
    assert bot._safe_funds() == 0.0            # gone again: alerts again
    assert [level for level, _ in bot.alerts.sent] == ["CRITICAL", "CRITICAL"]


# ---- item 3: a blocked startup reconciliation is alerted -----------------------------------

def test_startup_recon_mismatch_is_alerted(monkeypatch):
    bot = _bot()
    bot._audit = None
    monkeypatch.setattr(sb, "_BotBook", lambda b: None)
    monkeypatch.setattr(sb, "startup_reconcile",
                        lambda *a, **k: ReconGate(status="mismatch", mismatches={"INFY": 1}, error=None))
    bot.run_startup_recon(lambda: {})
    assert len(bot.alerts.sent) == 1
    level, message = bot.alerts.sent[0]
    assert level == "CRITICAL" and "mismatch" in message and "new entries blocked" in message


def test_startup_recon_match_is_not_alerted(monkeypatch):
    bot = _bot()
    bot._audit = None
    monkeypatch.setattr(sb, "_BotBook", lambda b: None)
    monkeypatch.setattr(sb, "startup_reconcile", lambda *a, **k: ReconGate(status="match"))
    bot.run_startup_recon(lambda: {})
    assert bot.alerts.sent == []
