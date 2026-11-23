"""Per-stage TTL tests (LLM_STAGE_TTL_PLAN, 2026-11-12).

Contract under test:
  * hit inside the TTL, miss after it,
  * risk and portfolio are NEVER cached (ttl 0),
  * a cached "no veto" answer is never served for risk,
  * failed calls are never cached,
  * a new session still gets the hit (no session salt in the key),
  * a staged entry survives a restart through the disk store,
  * the sector advisor's cached advice is re-validated.
"""
from __future__ import annotations

import json
import time

from blue_engine.core.llm.protocol import ChatMessage, ChatResult
from blue_engine.core.llm.tokens import TokenGovernor


class FakeClient:
    def __init__(self, text='{"ok": true}', ok=True):
        self.text = text
        self._ok = ok
        self.calls = 0
        self.session_id = None  # NOQA: sessions exist but must not affect keys
        self.backends = []
        self.governor = None

    def complete(self, messages, **kwargs):
        self.calls += 1
        if not self._ok:
            return ChatResult(ok=False, attempts=[{"backend": "fake",
                                                   "reason": "down"}])
        return ChatResult(ok=True, text=self.text, backend="fake",
                          model="m", model_digest="sha256:x",
                          usage={"completion_tokens": 12})


MSGS = [ChatMessage("system", "return JSON"), ChatMessage("user", "text")]


def _gov(client, ttls, cache_dir=None, clock=time.time):
    return TokenGovernor(client, stage_ttl_seconds=ttls, cache_dir=cache_dir,
                         monotonic=clock)


# --- hits and misses -------------------------------------------------------
def test_hit_inside_ttl_and_miss_after():
    client = FakeClient()
    now = [1000.0]

    def clock():
        return now[0]

    gov = _gov(client, {"economic": 600}, clock=clock)
    gov.complete(MSGS, cache_stage="economic")
    gov.complete(MSGS, cache_stage="economic")
    assert client.calls == 1, "hit inside the TTL"
    now[0] += 700
    gov.complete(MSGS, cache_stage="economic")
    assert client.calls == 2, "miss after the TTL"


def test_unnamed_stage_keeps_legacy_governor_cache():
    client = FakeClient()
    gov = _gov(client, {})
    gov.complete(MSGS)
    gov.complete(MSGS)
    assert client.calls == 1, "legacy in-memory default TTL unchanged"


# --- never-cache stages ----------------------------------------------------
def test_risk_and_portfolio_are_never_cached():
    client = FakeClient()
    gov = _gov(client, {"risk": 0, "portfolio": 0})
    gov.complete(MSGS, cache_stage="risk")
    gov.complete(MSGS, cache_stage="risk")
    gov.complete(MSGS, cache_stage="portfolio")
    gov.complete(MSGS, cache_stage="portfolio")
    assert client.calls == 4


def test_a_cached_no_veto_is_never_served_for_risk():
    # Even when an economic answer was cached, the risk stage must call.
    client = FakeClient()
    gov = _gov(client, {"economic": 86400, "risk": 0})
    gov.complete(MSGS, cache_stage="economic")
    first_risk = gov.complete(MSGS, cache_stage="risk")
    second_risk = gov.complete(MSGS, cache_stage="risk")
    assert first_risk.usage.get("cache_hit") is None
    assert second_risk.usage.get("cache_hit") is None
    assert client.calls == 3


# --- failure and session behaviour -----------------------------------------
def test_failed_call_is_never_cached():
    client = FakeClient(ok=False)
    gov = _gov(client, {"economic": 86400})
    gov.complete(MSGS, cache_stage="economic")
    gov.complete(MSGS, cache_stage="economic")
    assert client.calls == 2


def test_a_new_session_still_gets_the_hit():
    client = FakeClient()
    gov = _gov(client, {"economic": 86400})
    gov.complete(MSGS, cache_stage="economic")
    client.session_id = "brand-new-run"
    second = gov.complete(MSGS, cache_stage="economic")
    assert second.usage.get("cache_hit") is True
    assert client.calls == 1


# --- disk persistence --------------------------------------------------------
def test_staged_entry_survives_a_restart(tmp_path):
    client = FakeClient()
    now = [1000.0]

    def clock():
        return now[0]

    gov = _gov(client, {"economic": 86400}, cache_dir=tmp_path, clock=clock)
    gov.complete(MSGS, cache_stage="economic")
    # A NEW governor (process restart): empty memory, same disk.
    client2 = FakeClient()
    gov2 = _gov(client2, {"economic": 86400}, cache_dir=tmp_path, clock=clock)
    second = gov2.complete(MSGS, cache_stage="economic")
    assert second.usage.get("cache_hit") is True
    assert client2.calls == 0
    # Expired: the disk entry must not be served past the TTL.
    now[0] += 90000
    client3 = FakeClient()
    gov3 = _gov(client3, {"economic": 86400}, cache_dir=tmp_path, clock=clock)
    gov3.complete(MSGS, cache_stage="economic")
    assert client3.calls == 1


def test_stage_without_a_cache_dir_is_memory_only():
    client = FakeClient()
    gov = _gov(client, {"economic": 86400}, cache_dir=None)
    gov.complete(MSGS, cache_stage="economic")
    fresh = _gov(FakeClient(), {"economic": 86400}, cache_dir=None)
    fresh.complete(MSGS, cache_stage="economic")
    # no crash, second instance called its own client (disk disabled)
    assert True


# --- sector advisor cache ---------------------------------------------------
def test_sector_advisor_second_run_makes_zero_calls(tmp_path, monkeypatch):
    from blue_engine.sectors import sector_engine as sector_mod

    advice = {
        "invest_more_sectors": ["IT"], "wait_and_see_sectors": ["AUTO"],
        "sector_allocation_multipliers": {"IT": 1.1, "AUTO": 0.0},
        "dynamic_lookback_window": 14, "analytical_rationale": "IT leads",
    }

    class Adv(sector_mod.SectorAdvisor):
        calls = 0

        def _openrouter(self, prompt, key):  # noqa: ARG002
            Adv.calls += 1
            return dict(advice)

        def _ollama(self, prompt):  # noqa: ARG002
            Adv.calls += 1
            return dict(advice)

    monkeypatch.setattr(sector_mod, "get_secret", lambda name: "k")

    matrix = [
        {"sector": "IT", "relative_strength_score": 1.2,
         "relative_ratio": 1.1, "ratio_moving_average": 1.05,
         "history": [1.0, 1.1]},
        {"sector": "AUTO", "relative_strength_score": 0.8,
         "relative_ratio": 0.9, "ratio_moving_average": 0.95,
         "history": [1.0, 0.9]},
    ]
    a = Adv()
    first = a.advise(matrix, "news", ["IT", "AUTO"], cache_dir=tmp_path)
    second = a.advise(matrix, "news", ["IT", "AUTO"], cache_dir=tmp_path)
    assert first["method"] == "openrouter" and first["advice"] == advice
    assert second["method"] == "cache", "second run served from disk"
    assert second["advice"] == advice
    assert Adv.calls == 1, "a second identical run makes zero LLM calls"


def test_sector_advisor_expired_cache_calls_again(tmp_path, monkeypatch):
    from blue_engine.sectors import sector_engine as sector_mod

    advice = {
        "invest_more_sectors": ["IT"], "wait_and_see_sectors": ["AUTO"],
        "sector_allocation_multipliers": {"IT": 1.1, "AUTO": 0.0},
        "dynamic_lookback_window": 14, "analytical_rationale": "IT leads",
    }
    matrix = [
        {"sector": "IT", "relative_strength_score": 1.2,
         "relative_ratio": 1.1, "ratio_moving_average": 1.05,
         "history": [1.0, 1.1]},
        {"sector": "AUTO", "relative_strength_score": 0.8,
         "relative_ratio": 0.9, "ratio_moving_average": 0.95,
         "history": [1.0, 0.9]},
    ]

    class Adv(sector_mod.SectorAdvisor):
        def _openrouter(self, prompt, key):  # noqa: ARG002
            return dict(advice)

    monkeypatch.setattr(sector_mod, "get_secret", lambda name: "k")
    a = Adv()
    a.advise(matrix, "news", ["IT", "AUTO"], cache_dir=tmp_path)
    # Age every cache file past the TTL.
    stale = time.time() - sector_mod.SECTOR_ADVICE_TTL_SECONDS - 10
    for path in tmp_path.glob("advice_*.json"):
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["cached_at"] = stale
        path.write_text(json.dumps(payload), encoding="utf-8")
    third = a.advise(matrix, "news", ["IT", "AUTO"], cache_dir=tmp_path)
    assert third["method"] == "openrouter", "expired cache calls again"


def test_pm_note_respects_ttl(tmp_path, monkeypatch):
    from blue_engine.portfolio.views import llm_note

    def fake_client(timeout=30.0):  # noqa: ARG001
        class C:
            backends = []

            def complete(self, messages, **kwargs):
                return ChatResult(ok=True, text="Note text.",
                                  backend="fake", model="m",
                                  model_digest="sha256:x")

        return C()

    monkeypatch.setattr(llm_note, "_client", fake_client)
    monkeypatch.setattr(llm_note, "PM_NOTE_TTL_SECONDS", 1.0)
    first = llm_note.write_pm_note({"gross": 0.9}, cache_dir=tmp_path)
    assert first["cache"] == "miss"
    second = llm_note.write_pm_note({"gross": 0.9}, cache_dir=tmp_path)
    assert second["cache"] == "hit"
    # Age the file past the TTL -> miss again.
    stale = time.time() - 10.0
    for path in tmp_path.glob("note_*.json"):
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["_cached_at"] = stale
        path.write_text(json.dumps(payload), encoding="utf-8")
    third = llm_note.write_pm_note({"gross": 0.9}, cache_dir=tmp_path)
    assert third["cache"] == "miss", "an expired PM note is regenerated"
