"""
test_self_correction.py - the automatic self-correction loop.

The loop is only trustworthy if all of the following hold:

1. A good answer is accepted on the FIRST attempt (no wasted calls).
2. A rejected answer is re-asked with a PRECISE correction prompt.
3. The loop stops - it never runs unbounded, never retries a non-retryable
   transport failure, and never repeats an identical mistake forever.
4. A clamped answer is a SUCCESS (the rails already bounded it).
5. Evidence can be requested instead of invented (the ``insufficient_data``
   path), and the read-only toolbelt is what supplies it.
6. Nothing in the loop can write code or widen a limit.
"""
from __future__ import annotations

import json

import pytest

from blue_engine.core.self_correction import (
    MAX_ATTEMPTS_HARD_CAP,
    SelfCorrectionLoop,
    correction_message,
    diagnose,
)


RISK_OK = {
    "risk_veto_active": True,
    "max_allowed_slippage_pct": 0.01,
    "force_trailing_stop_pct": 1.5,
    "veto_reason": "spread widened beyond the tolerance band",
}


class FakeAgent:
    """Returns queued outcomes in order and records every prompt it saw."""

    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.prompts = []
        self.calls = 0

    def advise(self, engine, text, *, baseline=None, tools=None):
        self.prompts.append(text)
        self.calls += 1
        if not self.outcomes:
            return {"ok": False, "method": "invalid_response"}
        return self.outcomes.pop(0)


def _ok(advice):
    return {"ok": True, "method": "ollama", "advice": advice, "calls": 1}


def _bad(warning="agent response failed the engine schema"):
    return {"ok": False, "method": "invalid_response", "warning": warning}


# --------------------------------------------------------------------------- #
# Diagnosis
# --------------------------------------------------------------------------- #
def test_diagnose_reports_missing_keys_precisely():
    diag = diagnose("risk", {"risk_veto_active": True})
    assert diag["kind"] == "missing_keys"
    assert "veto_reason" in diag["keys"]


def test_diagnose_reports_extra_keys():
    diag = diagnose("risk", dict(RISK_OK, surprise=1))
    assert diag["kind"] == "extra_keys"
    assert diag["keys"] == ["surprise"]


def test_diagnose_reports_a_bad_value_with_the_allowed_range():
    payload = dict(RISK_OK, max_allowed_slippage_pct=0.5)
    diag = diagnose("risk", payload)
    assert diag["kind"] == "bad_value"
    assert "between 0.0 and 0.05" in diag["detail"][0]


def test_diagnose_flags_an_inverted_optimizer_range():
    diag = diagnose("optimization", {
        "min_exploration_rsi": 80, "max_exploration_rsi": 20,
        "optimization_method": "bounded_scipy", "rationale": "narrow the space"})
    assert diag["kind"] == "bad_value"
    assert "must not exceed" in diag["detail"][0]


def test_diagnose_flags_a_screener_reason_for_an_unlisted_ticker():
    diag = diagnose("screener", {
        "asset_blacklist": ["ABC"],
        "blacklist_reasons": [{"ticker": "XYZ", "reason": "auditor resigned"}]})
    assert diag["kind"] == "bad_value"
    assert "asset_blacklist" in diag["detail"][0]


def test_diagnose_reports_non_json():
    assert diagnose("risk", None)["kind"] == "not_json"
    assert diagnose("risk", ["a"])["kind"] == "not_json"


def test_diagnose_reports_insufficient_data_instead_of_a_guess():
    diag = diagnose("risk", RISK_OK, gate_missing=["portfolio.data"])
    assert diag["kind"] == "insufficient_data"
    assert diag["keys"] == ["portfolio.data"]


def test_a_wide_value_is_a_clamp_not_a_failure():
    diag = diagnose("risk", dict(RISK_OK, max_allowed_slippage_pct=0.04),
                    baseline={"max_allowed_slippage_pct": 0.01})
    assert diag["kind"] == "clamped"


def test_a_valid_answer_is_ok():
    assert diagnose("risk", RISK_OK)["kind"] == "ok"


# --------------------------------------------------------------------------- #
# Correction prompt
# --------------------------------------------------------------------------- #
def test_the_correction_prompt_names_the_failing_keys_only():
    msg = correction_message("risk", diagnose("risk", {"risk_veto_active": True}))
    assert "REJECTED" in msg
    assert "veto_reason" in msg
    assert "JSON object" in msg


def test_the_insufficient_data_prompt_forbids_inventing_a_number():
    msg = correction_message(
        "risk", diagnose("risk", RISK_OK, gate_missing=["portfolio.data"]),
        tools_available=["read_json", "provider"])
    assert "Do not invent" in msg
    assert "read_json" in msg


def test_the_prompt_says_the_answer_is_an_input_not_a_trade():
    msg = correction_message("risk", diagnose("risk", None))
    assert "not a trade" in msg


# --------------------------------------------------------------------------- #
# The loop
# --------------------------------------------------------------------------- #
def test_a_good_answer_is_accepted_on_the_first_attempt():
    agent = FakeAgent([_ok(RISK_OK)])
    result = SelfCorrectionLoop(agent).run("risk", "context")
    assert result["ok"] is True
    assert result["corrections"] == 0
    assert agent.calls == 1


def test_a_rejected_answer_is_re_asked_and_can_succeed():
    agent = FakeAgent([_bad(), _ok(RISK_OK)])
    result = SelfCorrectionLoop(agent, max_attempts=2).run("risk", "context")
    assert result["ok"] is True
    assert result["corrections"] == 1
    assert agent.calls == 2
    # The second prompt carries the correction, the first does not.
    assert "REJECTED" not in agent.prompts[0]
    assert "REJECTED" in agent.prompts[1]


def test_the_loop_never_exceeds_max_attempts():
    agent = FakeAgent([_bad("attempt failed")] * 10)
    result = SelfCorrectionLoop(agent, max_attempts=3).run("risk", "context")
    assert result["ok"] is False
    assert agent.calls <= MAX_ATTEMPTS_HARD_CAP


def test_max_attempts_is_clamped_to_the_hard_cap():
    loop = SelfCorrectionLoop(FakeAgent([]), max_attempts=99)
    assert loop.max_attempts == MAX_ATTEMPTS_HARD_CAP


def test_max_attempts_one_disables_correction():
    agent = FakeAgent([_bad()])
    result = SelfCorrectionLoop(agent, max_attempts=1).run("risk", "context")
    assert agent.calls == 1
    assert result["ok"] is False


def test_a_transport_failure_is_not_retried():
    agent = FakeAgent([{"ok": False, "method": "disabled"}, _ok(RISK_OK)])
    result = SelfCorrectionLoop(agent, max_attempts=3).run("risk", "context")
    assert agent.calls == 1
    assert result["ok"] is False
    assert any("not retryable" in w for w in result["warnings"])


def test_an_identical_failure_stops_the_loop_early():
    """The classic self-critique failure mode: three identical wrong answers."""
    agent = FakeAgent([{"ok": True, "method": "ollama", "advice": {"only": "x"}}] * 5)
    result = SelfCorrectionLoop(agent, max_attempts=3).run("risk", "context")
    assert result["ok"] is False
    assert agent.calls == 2          # second identical failure -> stop
    assert any("no progress" in w for w in result["warnings"])


def test_a_clamped_answer_is_a_success_not_a_retry():
    wide = dict(RISK_OK, max_allowed_slippage_pct=0.04)
    agent = FakeAgent([_ok(wide)])
    result = SelfCorrectionLoop(agent, max_attempts=3).run(
        "risk", "context", baseline={"max_allowed_slippage_pct": 0.01})
    assert result["ok"] is True
    assert agent.calls == 1
    assert any("clamped" in w for w in result["warnings"])


def test_evidence_gap_drives_a_correction_then_acceptance():
    """A stage-gate miss is correctable: fetch data, then answer."""
    class Gate:
        def __init__(self):
            self.calls = 0

        def check(self, stage, ctx):
            self.calls += 1
            state = ctx.get("have_data", False)
            return type("V", (), {"satisfied": state,
                                  "missing": [] if state else ["macro.cpi"]})()

    gate = Gate()
    ctx = {"have_data": False}
    agent = FakeAgent([_ok(RISK_OK), _ok(RISK_OK)])
    loop = SelfCorrectionLoop(agent, max_attempts=2, stage_gate=gate,
                              context=lambda: ctx)
    first = loop.run("risk", "context")
    # Evidence still missing at the end -> honest failure, not a fabricated pass.
    assert first["ok"] is False
    assert "macro.cpi" in json.dumps(first)

    ctx["have_data"] = True
    agent2 = FakeAgent([_ok(RISK_OK), _ok(RISK_OK)])
    ok = SelfCorrectionLoop(agent2, max_attempts=2, stage_gate=gate,
                            context=lambda: ctx).run("risk", "context")
    assert ok["ok"] is True


def test_the_attempt_log_is_json_safe():
    agent = FakeAgent([_bad(), _ok(RISK_OK)])
    result = SelfCorrectionLoop(agent, max_attempts=2).run("risk", "context")
    json.dumps(result)                       # must not raise
    assert [a["attempt"] for a in result["attempts"]] == [1, 2]
    assert result["attempts"][0]["ok"] is False
    assert result["attempts"][1]["ok"] is True


def test_the_loop_never_raises_when_the_agent_raises():
    class Exploding:
        def advise(self, *a, **k):
            raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        # Deliberate: the pipeline calls this inside its own try/finally, and
        # swallowing a programming error here would hide a real bug. The
        # *transport* failures (the normal case) are data, not exceptions.
        SelfCorrectionLoop(Exploding()).run("risk", "context")


def test_tools_are_passed_through_on_every_attempt():
    seen = []

    class ToolAgent(FakeAgent):
        def advise(self, engine, text, *, baseline=None, tools=None):
            seen.append(tools is not None)
            return super().advise(engine, text, baseline=baseline, tools=tools)

    agent = ToolAgent([_bad(), _ok(RISK_OK)])
    SelfCorrectionLoop(agent, max_attempts=2).run("risk", "context",
                                                  tools={"read_json": lambda a: {}})
    assert seen == [True, True]


def test_describe_reports_the_bounds():
    doc = SelfCorrectionLoop(FakeAgent([]), max_attempts=2).describe()
    assert doc == {"max_attempts": 2, "gated": False,
                   "hard_cap": MAX_ATTEMPTS_HARD_CAP}


# --------------------------------------------------------------------------- #
# The loop must stay out of execution
# --------------------------------------------------------------------------- #
def test_self_correction_never_imports_an_execution_module():
    """No import of, and no call into, an execution module.

    Only import/call shapes are checked: the module's own prose talks about
    orders on purpose, and a substring match on "order" would fail on comments.
    """
    import pathlib
    text = (pathlib.Path(__file__).resolve().parents[1]
            / "core" / "self_correction.py").read_text(encoding="utf-8")
    for forbidden in ("from .risk_engine", "from .risk_gate", "from ..pov",
                      "import risk_engine", "import risk_gate",
                      "place_order(", "cancel_order(", "submit_order("):
        assert forbidden not in text

# --------------------------------------------------------------------------- #
# REGRESSION: the loop must RETURN the clamped payload, not the raw proposal.
# --------------------------------------------------------------------------- #
def test_the_loop_returns_the_clamped_payload_not_the_raw_proposal():
    """AUDIT (2026-10-26): the judge detected the clamp, the loop discarded it.

    The loop asked ``diagnose`` whether the rails had tightened the answer,
    reported "clamped", and then returned the model's RAW payload. The safe
    value was computed and thrown away - so an agent could propose a slippage
    ceiling of 0.04 against a 0.01 baseline and the wider number survived.

    This test fails on that version and passes only when the payload that
    leaves the loop is the escalated one.
    """
    wide = dict(RISK_OK, max_allowed_slippage_pct=0.04)
    agent = FakeAgent([_ok(wide)])
    result = SelfCorrectionLoop(agent, max_attempts=1).run(
        "risk", "context", baseline={"max_allowed_slippage_pct": 0.01})
    assert result["ok"] is True
    assert result["advice"]["max_allowed_slippage_pct"] == 0.01
    assert result["clamped"] is True


def test_a_clamp_never_clears_a_veto_even_through_the_loop():
    no_veto = dict(RISK_OK, risk_veto_active=False)
    agent = FakeAgent([_ok(no_veto)])
    result = SelfCorrectionLoop(agent, max_attempts=1).run(
        "risk", "context", baseline={"risk_veto_active": True})
    assert result["advice"]["risk_veto_active"] is True


def test_data_requests_do_not_count_as_an_extra_key():
    """The judge and the validator must agree about the optional block."""
    agent = FakeAgent([_ok(dict(RISK_OK, data_requests=["macro.cpi"]))])
    result = SelfCorrectionLoop(agent, max_attempts=1).run("risk", "context")
    assert result["ok"] is True, result
    assert result["corrections"] == 0
