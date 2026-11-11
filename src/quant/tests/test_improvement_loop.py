"""Improvement loop: the challenger gate must recommend and never promote.

Flow stage: Champion/challenger
Status: TEST

Every test here has a PASS path and a BLOCK path. The block paths matter most:
an improvement loop that is too eager is a curve-fitting machine wired to
capital.
"""
import ast
import pathlib

from quant.feedback.improvement_loop import (
    MAX_PBO,
    MIN_OOS_OBS,
    MIN_SHARPE_GAIN,
    ImprovementLoop,
    compare,
    evaluate_and_record,
)

ROOT = pathlib.Path(__file__).resolve().parents[3]

CHAMPION = {"sharpe": 1.20, "dsr": 0.80, "pbo": 0.20, "n_oos": 400}
BETTER = {"sharpe": 1.75, "dsr": 0.90, "pbo": 0.20, "n_oos": 400}


class _Audit:
    def __init__(self):
        self.rows = []

    def append(self, kind, payload):
        self.rows.append((kind, payload))


# --------------------------------------------------------------------------- #
# PASS path
# --------------------------------------------------------------------------- #
def test_a_clearly_better_challenger_is_recommended():
    out = compare(CHAMPION, BETTER)
    assert out.verdict == "promote_recommended"
    assert out.promote_recommended is True
    assert "advice only" in " ".join(out.reasons)


def test_the_recommendation_carries_the_evidence():
    out = compare(CHAMPION, BETTER)
    assert out.metrics["challenger"]["sharpe"] == 1.75
    assert out.metrics["champion"]["sharpe"] == 1.20


# --------------------------------------------------------------------------- #
# BLOCK paths - one per rule
# --------------------------------------------------------------------------- #
def test_thin_evidence_is_insufficient_not_a_win():
    thin = {**BETTER, "n_oos": MIN_OOS_OBS - 1}
    out = compare(CHAMPION, thin)
    assert out.verdict == "insufficient"
    assert out.promote_recommended is False
    assert any("insufficient out-of-sample" in r for r in out.reasons)


def test_a_tiny_sharpe_gain_is_inside_the_noise():
    marginal = {**BETTER, "sharpe": CHAMPION["sharpe"] + MIN_SHARPE_GAIN / 2}
    out = compare(CHAMPION, marginal)
    assert out.verdict == "keep_champion"
    assert any("inside backtest noise" in r for r in out.reasons)


def test_high_pbo_vetoes_even_a_better_score():
    overfit = {**BETTER, "pbo": MAX_PBO + 0.1}
    out = compare(CHAMPION, overfit)
    assert out.verdict == "keep_champion"
    assert any("overfitting" in r for r in out.reasons)


def test_dsr_regression_blocks_promotion():
    worse_dsr = {**BETTER, "dsr": 0.10}
    out = compare(CHAMPION, worse_dsr)
    assert out.verdict == "keep_champion"
    assert any("DSR regresses" in r for r in out.reasons)


def test_missing_pbo_blocks_rather_than_passing_silently():
    out = compare(CHAMPION, {k: v for k, v in BETTER.items() if k != "pbo"})
    assert out.verdict == "insufficient"
    assert any("pbo missing" in r for r in out.reasons)


def test_missing_dsr_blocks_rather_than_passing_silently():
    out = compare(CHAMPION, {k: v for k, v in BETTER.items() if k != "dsr"})
    assert out.verdict == "insufficient"
    assert any("dsr missing" in r for r in out.reasons)


def test_nan_and_inf_are_not_numbers_here():
    broke = {**BETTER, "sharpe": float("nan")}
    out = compare(CHAMPION, broke)
    assert out.promote_recommended is False

    broke = {**BETTER, "sharpe": float("inf")}
    out = compare(CHAMPION, broke)
    assert out.promote_recommended is False


def test_a_bool_is_not_accepted_as_a_metric():
    """True == 1 in Python; a boolean leaking into a metric must not score."""
    out = compare(CHAMPION, {**BETTER, "sharpe": True})
    assert out.promote_recommended is False


# --------------------------------------------------------------------------- #
# The rule that must never break: advice only
# --------------------------------------------------------------------------- #
def test_this_module_never_calls_promote():
    """A regression that wires promote() into the loop is the whole risk."""
    src = (ROOT / "src" / "quant" / "feedback" / "improvement_loop.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    calls = [n for n in ast.walk(tree)
             if isinstance(n, ast.Attribute) and n.attr == "promote"]
    assert not calls, "improvement_loop must recommend, never promote"
    imports = [n for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom))]
    names = {getattr(n, "module", None) for n in imports} | {
        a.name for n in imports for a in n.names}
    assert "champion_challenger" not in names, "the loop must not import the registry"


def test_evaluation_writes_one_audit_row():
    audit = _Audit()
    evaluate_and_record("STRAT-1", CHAMPION, BETTER, audit=audit)
    assert len(audit.rows) == 1
    kind, payload = audit.rows[0]
    assert kind == "challenger_compared"
    assert payload["strategy_id"] == "STRAT-1"
    assert payload["advice_only"] is True


def test_loop_runs_a_batch_and_summarises():
    loop = ImprovementLoop()
    rows = loop.run([
        {"strategy_id": "A", "champion": CHAMPION, "challenger": BETTER},
        {"strategy_id": "B", "champion": CHAMPION,
         "challenger": {**CHAMPION, "n_oos": 10}},
    ])
    assert len(rows) == 2
    assert loop.summary() == {"promote_recommended": 1, "keep_champion": 0,
                              "insufficient": 1}


def test_a_missing_challenger_dict_does_not_raise():
    """A config typo must produce a verdict, not an exception on the trade path."""
    out = compare(CHAMPION, {})
    assert out.verdict == "insufficient"
    assert out.promote_recommended is False
