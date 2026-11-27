"""Sandbox circuit fallback (owner report 2026-11-27: every name dropped in analyze mode).

Rules under test:
1. The fallback band applies ONLY when the sandbox quote has no circuit limits AND the analyzer
   is confirmed ON for the refresh. Live (analyzer off or unknown) keeps the strict rule.
2. An asymmetric or implausible broker band is still rejected, even in the sandbox.
3. ASM/GSM is NOT relaxed: a sandbox quote that does not publish the flag still excludes the name.
4. The analyzer query is read-only and returns True / False / None, never a guessed yes.
"""
from datetime import datetime

from quant.data import screen_refresh as sr
from quant.data.circuit_band import SANDBOX_CIRCUIT_PCT, enrich_candidates
from quant.execution.readonly_client import OpenAlgoReadOnly

NOW = datetime(2026, 11, 27, 10, 0)


def _reply(data):
    return {"status": "success", "data": data}


def test_fallback_only_when_sandbox_flag_given():
    no_limits = lambda s, e: _reply({"ltp": 100.0, "asm": False})
    strict = enrich_candidates([{"symbol": "INFY"}], no_limits, exchange="NSE")
    assert strict["candidates"][0]["circuit_pct"] is None
    assert strict["candidates"][0]["band_source"] == "broker"
    sandbox = enrich_candidates([{"symbol": "INFY"}], no_limits, exchange="NSE",
                                sandbox_circuit_pct=SANDBOX_CIRCUIT_PCT)
    row = sandbox["candidates"][0]
    assert row["circuit_pct"] == SANDBOX_CIRCUIT_PCT
    assert row["band_source"] == "sandbox_default"
    assert row["asm_gsm"] is False


def test_asymmetric_broker_band_still_rejected_in_sandbox():
    lopsided = lambda s, e: _reply({"upper_circuit_limit": 120.0, "lower_circuit_limit": 99.0,
                                    "prev_close": 100.0, "asm": False})
    out = enrich_candidates([{"symbol": "X"}], lopsided, exchange="NSE",
                            sandbox_circuit_pct=SANDBOX_CIRCUIT_PCT)["candidates"][0]
    assert out["circuit_pct"] is None
    assert out["band_source"] == "broker"


def test_asm_not_relaxed_in_sandbox():
    no_flag = lambda s, e: _reply({"ltp": 100.0})
    out = enrich_candidates([{"symbol": "INFY"}], no_flag, exchange="NSE",
                            sandbox_circuit_pct=SANDBOX_CIRCUIT_PCT)["candidates"][0]
    assert out["circuit_pct"] == SANDBOX_CIRCUIT_PCT
    assert out["asm_gsm"] is None


class _Transport:
    def __init__(self, reply=None, error=None):
        self.reply, self.error = reply, error
        self.calls = []

    def __call__(self, url, payload):
        self.calls.append((url, payload))
        if self.error:
            raise self.error
        return self.reply


def test_analyzer_mode_is_true_false_or_none():
    on = OpenAlgoReadOnly("http://x", _Transport(_reply({"analyze_mode": True})))
    off = OpenAlgoReadOnly("http://x", _Transport(_reply({"analyze_mode": False})))
    unclear = OpenAlgoReadOnly("http://x", _Transport({"status": "error"}))
    broken = OpenAlgoReadOnly("http://x", _Transport(error=RuntimeError("down")))
    assert on.analyzer_mode("k") is True
    assert off.analyzer_mode("k") is False
    assert unclear.analyzer_mode("k") is None
    assert broken.analyzer_mode("k") is None
    assert on.analyzer_mode("") is None


class _Reader:
    """Read-only fake: quotes carry no circuit limits, but publish the ASM flag."""

    def __init__(self, analyze):
        self.analyze = analyze

    def quotes(self, api_key, symbol, exchange):
        return _reply({"ltp": 100.0, "prev_close": 100.0, "asm": False})

    def analyzer_mode(self, api_key):
        return self.analyze


def _run_refresh(tmp_path, analyze):
    tmp_path.mkdir(parents=True, exist_ok=True)
    source = tmp_path / "last_screen.json"
    source.write_text('{"results": [{"symbol": "INFY", "selected": true}]}', encoding="utf-8")
    target = tmp_path / "latest.json"
    sr.make_screen_refresher(_Reader(analyze), "key", source=source, target=target)(NOW)
    import json
    return json.loads(target.read_text(encoding="utf-8"))["results"]


def test_chain_keeps_names_in_sandbox_and_drops_them_live(tmp_path):
    in_sandbox = _run_refresh(tmp_path / "a", analyze=True)
    assert [r["symbol"] for r in in_sandbox if r.get("selected")] == ["INFY"]
    assert in_sandbox[0]["source"] == "screener"
    live = _run_refresh(tmp_path / "b", analyze=False)
    assert not any(r.get("selected") for r in live)
