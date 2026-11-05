"""X-17: event scoring works without an LLM. LLM output is advice only.

Flow stage: Event scoring (models/events/scoring.py). Status: TEST.
Hand check: "order win" (+1.0) and "buyback" (+0.5) sum to 1.5, clamped to 1.0.
"""
import logging

import pytest

from quant.models import events as ev

TEXT = "Company bags order win and announces buyback"


def test_keyword_score_is_deterministic_and_clamped():
    a = ev.score_filing_advice("A", TEXT)
    assert a.source == "deterministic"
    assert a.score == 1.0
    assert a.advice_score == 1.0
    assert a.advice_source == "deterministic"


def test_negative_keyword_score():
    assert ev.score_filing_advice("A", "fraud alleged").score == -1.0


def test_llm_score_used_when_it_works():
    a = ev.score_filing_advice("A", TEXT, llm_scorer=lambda s, t: 0.4)
    assert a.source == "llm"
    assert a.score == pytest.approx(0.4)
    assert a.matched == ()


def test_llm_score_is_clamped():
    a = ev.score_filing_advice("A", TEXT, llm_scorer=lambda s, t: 5.0)
    assert a.source == "llm"
    assert a.score == 1.0


def test_llm_exception_falls_back_with_warning(caplog):
    def boom(_symbol, _text):
        raise RuntimeError("ollama down")

    with caplog.at_level(logging.WARNING):
        a = ev.score_filing_advice("A", TEXT, llm_scorer=boom)
    assert a.source == "deterministic"
    assert a.score == 1.0
    assert any(r.levelno == logging.WARNING for r in caplog.records)


@pytest.mark.parametrize("bad", [float("nan"), float("inf")])
def test_non_finite_llm_output_falls_back(bad):
    a = ev.score_filing_advice("A", "fraud", llm_scorer=lambda s, t: bad)
    assert a.source == "deterministic"
    assert a.score == -1.0


def test_advice_never_sets_size():
    for scorer in (None, lambda s, t: 0.9):
        assert ev.score_filing_advice("A", TEXT, llm_scorer=scorer).sets_size is False
