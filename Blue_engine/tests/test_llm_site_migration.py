"""Tests that the three legacy LLM call sites use the shared chain.

Migration goal (2026-10-29): `llm_scorer`, `llm_note` and `SectorAdvisor` no
longer own a transport. They must
  1. build their chain through LLMClient (so failover + quota rules apply),
  2. keep their own content-hash DISK cache and schema validation,
  3. keep degrading to a deterministic fallback, never raising,
  4. NOT get the in-memory governor cache unless they opt in.
"""
from __future__ import annotations

import json

from blue_engine.core.llm import LLMClient
from blue_engine.core.llm.protocol import ChatResult
from blue_engine.economics.nlp import llm_scorer as scorer_mod
from blue_engine.portfolio.views import llm_note
from blue_engine.sectors import sector_engine as sector_mod

_ADVICE = {
    "invest_more_sectors": ["NIFTY IT"],
    "wait_and_see_sectors": ["NIFTY AUTO"],
    "sector_allocation_multipliers": {"NIFTY IT": 1.1, "NIFTY AUTO": 0.0},
    "dynamic_lookback_window": 14,
    "analytical_rationale": "IT leads on relative strength",
}

_SCORE = {"hawkish": 0.3, "inflation_focus": 0.7, "growth_focus": 0.4,
          "guidance": "hawkish", "confidence": 0.6}


class FakeClient(LLMClient):
    """A client over one scripted backend."""

    def __init__(self, text="", ok=True):
        from blue_engine.core.llm.protocol import ChatMessage  # noqa: F401
        self.backends = [_Backend(text or json.dumps(_ADVICE), ok)]
        self.session_id = None
        self.governor = None
        self.calls = 0

    def complete(self, messages, **kwargs):
        self.calls += 1
        return self.backends[0].complete(messages, **kwargs)


class _Backend:
    name = "fake"

    def __init__(self, text, ok):
        self.text = text
        self._ok = ok

    def available(self):
        return True

    def status(self):
        return {"backend": self.name}

    def complete(self, messages, **kwargs):
        if not self._ok:
            return ChatResult(ok=False, backend=self.name,
                              attempts=[{"backend": self.name, "reason": "down"}])
        return ChatResult(ok=True, text=self.text, backend=self.name,
                          model="fake", model_digest="sha256:fake")


# --- governor opt-in policy ----------------------------------------------
def test_named_site_has_no_in_memory_cache_by_default():
    client = LLMClient.for_named_site([_Backend("{}", True)])
    assert client.governor is None


def test_named_site_can_opt_into_governance():
    client = LLMClient.for_named_site([_Backend("{}", True)],
                                      {"tokens": {"enabled": True}})
    assert client.governor is not None


# --- llm_scorer -----------------------------------------------------------
def test_scorer_uses_the_shared_client_and_keeps_its_schema(tmp_path):
    client = FakeClient(json.dumps(_SCORE))
    scorer = scorer_mod.LLMScorer(client=client, cache_dir=tmp_path)
    out = scorer.score("RBI holds rates, hawkish tone", use_cache=False)
    assert out["method"] == "llm" and out["guidance"] == "hawkish"
    assert client.calls == 1


def test_scorer_still_caches_to_disk(tmp_path):
    client = FakeClient(json.dumps(_SCORE))
    scorer = scorer_mod.LLMScorer(client=client, cache_dir=tmp_path)
    scorer.score("same document")
    out = scorer.score("same document")
    assert out["method"] == "cached"
    assert client.calls == 1, "second call must be served from disk"


def test_scorer_rejects_out_of_schema_json(tmp_path):
    client = FakeClient(json.dumps({"hawkish": 0.1}))
    scorer = scorer_mod.LLMScorer(client=client, cache_dir=tmp_path)
    out = scorer.score("text", use_cache=False)
    assert out["method"] == "invalid_response"


def test_scorer_reports_a_chain_failure_without_raising(tmp_path):
    client = FakeClient("", ok=False)
    scorer = scorer_mod.LLMScorer(client=client, cache_dir=tmp_path)
    out = scorer.score("text", use_cache=False)
    assert out["method"] == "error"


def test_scorer_with_no_backend_is_disabled(tmp_path):
    scorer = scorer_mod.LLMScorer(
        client=LLMClient([]), cache_dir=tmp_path)
    assert scorer.enabled() is False
    assert scorer.score("text")["method"] == "disabled"


# --- llm_note -------------------------------------------------------------
def test_pm_note_uses_the_chain_then_caches(tmp_path, monkeypatch):
    monkeypatch.setattr(llm_note, "_client",
                        lambda timeout=30.0: FakeClient("Gross exposure 0.9x."))
    first = llm_note.write_pm_note({"gross": 0.9}, cache_dir=tmp_path)
    second = llm_note.write_pm_note({"gross": 0.9}, cache_dir=tmp_path)
    assert first["ok"] is True and first["method"] == "fake"
    assert second["cache"] == "hit"


def test_pm_note_falls_back_to_the_template_when_the_chain_fails(tmp_path,
                                                                monkeypatch):
    monkeypatch.setattr(llm_note, "_client",
                        lambda timeout=30.0: FakeClient("", ok=False))
    out = llm_note.write_pm_note(
        {"gross": 0.9, "regime": "RISK_ON"}, cache_dir=tmp_path)
    assert out["method"] == "template"
    assert "RISK_ON" in out["note"]


def test_extract_views_parses_a_json_array_from_any_backend(tmp_path,
                                                            monkeypatch):
    payload = json.dumps([{"sleeve": "NIFTY", "view_return": 0.01,
                           "confidence": 0.6, "horizon_days": 20,
                           "rationale": "test"}])
    monkeypatch.setattr(llm_note, "_client",
                        lambda timeout=30.0: FakeClient(payload))
    out = llm_note.extract_views("text", sleeves=["NIFTY"], cache_dir=tmp_path)
    assert out["ok"] is True and out["views"], "one view expected"


def test_available_reports_the_shared_backends(monkeypatch):
    monkeypatch.setattr(llm_note, "_secret",
                        lambda name: "x" if name == "OLLAMA_HOST" else None)
    assert llm_note.available()["method"] == "ollama"
    monkeypatch.setattr(llm_note, "_secret",
                        lambda name: None if name != "OPENROUTER_API_KEY" else "k")
    assert llm_note.available()["method"] == "openrouter"


# --- sector advisor -------------------------------------------------------
def test_sector_ask_parses_fenced_json_from_the_backend(monkeypatch):
    advisor = sector_mod.SectorAdvisor()
    fenced = "```json\n" + json.dumps(_ADVICE) + "\n```"
    monkeypatch.setattr(sector_mod.LLMClient, "for_named_site",
                        classmethod(lambda cls, b: FakeClient(fenced)))
    out = advisor._ask("prompt", _Backend("", True), "m")
    assert out == _ADVICE


def test_sector_ask_returns_none_on_a_chain_failure(monkeypatch):
    advisor = sector_mod.SectorAdvisor()
    monkeypatch.setattr(sector_mod.LLMClient, "for_named_site",
                        classmethod(lambda cls, b: FakeClient("", ok=False)))
    assert advisor._ask("prompt", _Backend("", True), "m") is None


def test_sector_ask_refuses_an_oversized_prompt():
    advisor = sector_mod.SectorAdvisor()
    assert advisor._ask("x" * (sector_mod._MAX_BODY_BYTES + 10),
                        _Backend("", True), "m") is None
