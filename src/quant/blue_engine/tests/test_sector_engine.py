from __future__ import annotations

import json
import math
import threading
import urllib.error
from concurrent.futures import ThreadPoolExecutor

from blue_engine.sectors.sector_engine import (
    SectorAdvisor,
    SectorEngine,
    _validate_advice,
)
from blue_engine.sectors import sector_engine as sector_engine_module


def _market_data(sectors=None):
    benchmark = [100 + i for i in range(20)]
    return {
        "benchmark_name": "NIFTY 500",
        "data_classification": "historical",
        "benchmark_closes": benchmark,
        "sector_closes": sectors or {
            "NIFTY IT": [100 + i * 1.2 for i in range(20)],
            "NIFTY AUTO": [100 + i * 0.8 for i in range(20)],
        },
    }


class _Advisor:
    def __init__(self, advice=None, method="openrouter"):
        self.advice = advice
        self.method = method
        self.calls = 0

    def advise(self, matrix, news, names):
        self.calls += 1
        self.matrix = matrix
        self.news = news
        self.names = names
        return {"method": self.method, "advice": self.advice}


def _valid_advice():
    return {
        "invest_more_sectors": ["NIFTY IT"],
        "wait_and_see_sectors": ["NIFTY AUTO"],
        "sector_allocation_multipliers": {"NIFTY IT": 1.1, "NIFTY AUTO": 0.0},
        "dynamic_lookback_window": 14,
        "analytical_rationale": "IT strength is positive; auto relative strength is weaker.",
    }


def test_sector_engine_calculates_mansfield_rs_and_persists_outputs(tmp_path):
    advisor = _Advisor(_valid_advice())
    engine = SectorEngine({
        "agent_enabled": True,
        "limits_path": tmp_path / "configs" / "sector_allocation_limits.json",
        "snapshot_path": tmp_path / "snapshot.json",
    }, advisor=advisor)

    result = engine.run({
        "sector_market_data": _market_data(),
        "sector_news_context": "Synthetic context only.",
    })

    assert result.ok
    assert result.data["advisor_method"] == "openrouter"
    assert result.data["invest_more_sectors"] == ["NIFTY IT"]
    assert result.data["sector_allocation_multipliers"]["NIFTY AUTO"] == 0.0
    assert result.data["dynamic_lookback_window"] == 14
    assert result.data["sector_matrix"][0]["sector"] == "NIFTY IT"
    ratios = [
        sector / benchmark
        for sector, benchmark in zip(
            _market_data()["sector_closes"]["NIFTY IT"],
            _market_data()["benchmark_closes"],
        )
    ]
    expected_score = ((ratios[-1] / (sum(ratios[-14:]) / 14)) - 1) * 10
    assert math.isclose(
        result.data["sector_matrix"][0]["relative_strength_score"],
        expected_score,
        abs_tol=1e-6,
    )
    assert advisor.news == "Synthetic context only."
    limits = json.loads((tmp_path / "configs" / "sector_allocation_limits.json").read_text())
    assert set(limits) == {
        "invest_more_sectors", "wait_and_see_sectors",
        "sector_allocation_multipliers", "dynamic_lookback_window",
        "analytical_rationale",
    }
    snapshot = json.loads((tmp_path / "snapshot.json").read_text())
    assert snapshot["data_classification"] == "historical"
    assert len(snapshot["sector_matrix"][0]["history"]) == 20


def test_sector_engine_uses_all_wait_safety_output_for_invalid_model_advice(tmp_path):
    advisor = _Advisor({"invalid": True}, method="safety_fallback")
    engine = SectorEngine({
        "agent_enabled": True,
        "limits_path": tmp_path / "limits.json",
        "snapshot_path": tmp_path / "snapshot.json",
    }, advisor=advisor)

    result = engine.run({"sector_market_data": _market_data()})
    assert result.ok
    assert result.data["invest_more_sectors"] == []
    assert set(result.data["wait_and_see_sectors"]) == {"NIFTY IT", "NIFTY AUTO"}
    # A defensive fallback HOLDS allocation (1.0), it does not liquidate it.
    # See SectorEngine._safe_limits for why 0.0 was a bug.
    assert set(result.data["sector_allocation_multipliers"].values()) == {1.0}
    assert result.data["dynamic_lookback_window"] == 14
    assert result.warnings


def test_sector_engine_keeps_agent_disabled_and_uses_defensive_limits(tmp_path):
    advisor = _Advisor(_valid_advice())
    engine = SectorEngine({
        "limits_path": tmp_path / "limits.json",
        "snapshot_path": tmp_path / "snapshot.json",
    }, advisor=advisor)

    result = engine.run({"sector_market_data": _market_data()})

    assert result.ok
    assert advisor.calls == 0
    assert result.data["advisor_method"] == "disabled"
    assert result.data["wait_and_see_sectors"] == ["NIFTY IT", "NIFTY AUTO"]
    # Defensive limits HOLD allocation (1.0); see SectorEngine._safe_limits.
    assert result.data["sector_allocation_multipliers"] == {
        "NIFTY IT": 1.0, "NIFTY AUTO": 1.0,
    }


def test_sector_engine_defensive_veto_skips_advisor(tmp_path):
    advisor = _Advisor(_valid_advice())
    engine = SectorEngine({
        "agent_enabled": True,
        "limits_path": tmp_path / "limits.json",
        "snapshot_path": tmp_path / "snapshot.json",
    }, advisor=advisor)

    result = engine.run({
        "sector_market_data": _market_data(),
        "tail_risk_panic": True,
    })

    assert result.ok
    assert advisor.calls == 0
    assert result.data["advisor_method"] == "safety_fallback"
    assert set(result.data["sector_allocation_multipliers"].values()) == {1.0}


def test_older_concurrent_run_cannot_overwrite_newer_limits(tmp_path):
    slow_started = threading.Event()
    release_slow = threading.Event()

    class _ConcurrentAdvisor:
        def advise(self, _matrix, news, _names):
            if news == "slow":
                slow_started.set()
                assert release_slow.wait(timeout=5)
                return {"method": "openrouter", "advice": _valid_advice()}
            return {
                "method": "openrouter",
                "advice": {
                    "invest_more_sectors": [],
                    "wait_and_see_sectors": ["NIFTY IT", "NIFTY AUTO"],
                    "sector_allocation_multipliers": {
                        "NIFTY IT": 0.0, "NIFTY AUTO": 0.0,
                    },
                    "dynamic_lookback_window": 14,
                    "analytical_rationale": "Newer concurrent run is defensive.",
                },
            }

    limits_path = tmp_path / "limits.json"
    snapshot_path = tmp_path / "snapshot.json"
    engine = SectorEngine({
        "agent_enabled": True,
        "limits_path": limits_path,
        "snapshot_path": snapshot_path,
    }, advisor=_ConcurrentAdvisor())
    with ThreadPoolExecutor(max_workers=2) as executor:
        older = executor.submit(engine.run, {
            "sector_market_data": _market_data(),
            "sector_news_context": "slow",
        })
        assert slow_started.wait(timeout=5)
        newer = engine.run({
            "sector_market_data": _market_data(),
            "sector_news_context": "new",
        })
        release_slow.set()
        older.result(timeout=5)

    assert newer.data["analytical_rationale"] == "Newer concurrent run is defensive."
    persisted = json.loads(limits_path.read_text())
    assert persisted["analytical_rationale"] == "Newer concurrent run is defensive."


def test_sector_engine_is_optional_macro_pipeline_stage(tmp_path):
    from blue_engine.core.pipeline import MacroPipeline

    pipeline = MacroPipeline({
        "dry_run": True,
        "gate_on_infrastructure": False,
        "engines": {
            "sector": {
                "pipeline_enabled": True,
                "limits_path": str(tmp_path / "limits.json"),
                "snapshot_path": str(tmp_path / "snapshot.json"),
            },
        },
    })

    report = pipeline.run({"sector_market_data": _market_data()})

    assert "sector" in report["order"]
    assert report["stages"]["sector"]["data"]["advisor_method"] == "disabled"
    assert report["stages"]["sector"]["data"]["wait_and_see_sectors"]


def test_sector_engine_rejects_bad_price_series(tmp_path):
    engine = SectorEngine({
        "limits_path": tmp_path / "limits.json",
        "snapshot_path": tmp_path / "snapshot.json",
    })
    data = _market_data()
    data["sector_closes"]["NIFTY IT"][0] = float("nan")

    result = engine.run({"sector_market_data": data})

    assert not result.ok
    assert "non-positive or non-finite" in result.error
    limits = json.loads((tmp_path / "limits.json").read_text())
    assert limits["wait_and_see_sectors"] == ["NIFTY IT", "NIFTY AUTO"]
    # Persisted defensive limits hold allocation rather than zeroing it, so a
    # single transient data gap cannot permanently cut the sleeve.
    assert set(limits["sector_allocation_multipliers"].values()) == {1.0}


def test_sector_advice_requires_exact_sector_partition():
    assert _validate_advice(_valid_advice(), ["NIFTY IT", "NIFTY AUTO"])
    malformed = _valid_advice()
    malformed["invest_more_sectors"] = [{"not": "hashable"}]
    assert _validate_advice(malformed, ["NIFTY IT", "NIFTY AUTO"]) is None
    incomplete = _valid_advice()
    incomplete["wait_and_see_sectors"] = []
    assert _validate_advice(incomplete, ["NIFTY IT", "NIFTY AUTO"]) is None
    assert _validate_advice(
        _valid_advice(),
        ["NIFTY IT", "NIFTY AUTO"],
        {"NIFTY IT": -0.1, "NIFTY AUTO": -0.2},
    ) is None


def test_sector_advisor_falls_back_to_ollama_with_four_second_timeouts(monkeypatch):
    monkeypatch.setattr(
        sector_engine_module, "get_secret", lambda name: "test-sector-key")
    seen = []

    class _Response:
        def __init__(self, payload):
            self.payload = payload

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _limit):
            return json.dumps(self.payload).encode("utf-8")

    def _urlopen(request, timeout):
        seen.append((request.full_url, timeout))
        if "openrouter.ai" in request.full_url:
            assert request.get_header("Authorization") == "Bearer test-sector-key"
            raise urllib.error.URLError("synthetic cloud outage")
        return _Response({
            "choices": [{"message": {"content": json.dumps(_valid_advice())}}],
        })

    monkeypatch.setattr(sector_engine_module.urllib.request, "urlopen", _urlopen)
    result = SectorAdvisor(ollama_host="http://127.0.0.1:11434").advise(
        [
            {"sector": "NIFTY IT", "relative_strength_score": 0.2,
             "relative_ratio": 1.1, "ratio_moving_average": 1.0, "history": [1.1]},
            {"sector": "NIFTY AUTO", "relative_strength_score": -0.2,
             "relative_ratio": 0.9, "ratio_moving_average": 1.0, "history": [0.9]},
        ],
        "synthetic context",
        ["NIFTY IT", "NIFTY AUTO"],
    )

    assert result["method"] == "ollama"
    assert result["advice"] == _valid_advice()
    # 2026-10-29: Ollama is reached through the shared backend (its
    # OpenAI-compatible /v1/chat/completions path), not the native /api/chat,
    # so one code path serves every provider. The 4s deadline is unchanged and
    # the order is still cloud-first, local-second.
    assert seen == [
        (SectorAdvisor.openrouter_endpoint, 4.0),
        ("http://127.0.0.1:11434/v1/chat/completions", 4.0),
    ]


def test_sector_advisor_accepts_valid_openrouter_response(monkeypatch, tmp_path):
    monkeypatch.setattr(
        sector_engine_module, "get_secret", lambda name: "test-sector-key")

    class _Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _limit):
            return json.dumps({
                "choices": [{"message": {"content": json.dumps(_valid_advice())}}],
            }).encode("utf-8")

    seen = []

    def _urlopen(request, timeout):
        seen.append((request.full_url, timeout))
        assert request.get_header("Authorization") == "Bearer test-sector-key"
        payload = json.loads(request.data.decode("utf-8"))
        assert payload["model"] == "qwen/qwen-2.5-7b-instruct:free"
        assert payload["messages"][0]["content"].startswith(
            "ROLE: Institutional Frontline Sector Engine Agent.")
        return _Response()

    monkeypatch.setattr(sector_engine_module.urllib.request, "urlopen", _urlopen)
    advisor = SectorAdvisor()
    monkeypatch.setattr(
        advisor, "_ollama",
        lambda *_args: (_ for _ in ()).throw(AssertionError("fallback not needed")))

    result = advisor.advise(
        [
            {"sector": "NIFTY IT", "relative_strength_score": 0.2,
             "relative_ratio": 1.1, "ratio_moving_average": 1.0, "history": [1.1]},
            {"sector": "NIFTY AUTO", "relative_strength_score": -0.2,
             "relative_ratio": 0.9, "ratio_moving_average": 1.0, "history": [0.9]},
        ],
        "synthetic context",
        ["NIFTY IT", "NIFTY AUTO"],
        cache_dir=tmp_path,
    )

    assert result["method"] == "openrouter"
    assert result["advice"] == _valid_advice()
    assert seen == [(SectorAdvisor.openrouter_endpoint, 4.0)]


def test_sector_advisor_never_sends_credential_like_context(monkeypatch):
    monkeypatch.setattr(
        sector_engine_module, "get_secret", lambda name: "test-sector-key")
    advisor = SectorAdvisor()
    monkeypatch.setattr(
        advisor, "_openrouter",
        lambda *_args: (_ for _ in ()).throw(AssertionError("cloud must not be called")))
    monkeypatch.setattr(
        advisor, "_ollama",
        lambda *_args: (_ for _ in ()).throw(AssertionError("local must not be called")))

    result = advisor.advise(
        [], "redacted test: sk-or-v1-abcdefghijklmnopqrstuvwxyz",
        ["NIFTY IT", "NIFTY AUTO"])

    assert result["method"] == "safety_fallback"
    assert "was not sent" in result["warning"]


def test_sector_engine_rejects_mismatched_series(tmp_path):
    engine = SectorEngine({
        "limits_path": tmp_path / "limits.json",
        "snapshot_path": tmp_path / "snapshot.json",
    })
    data = _market_data()
    data["sector_closes"]["NIFTY IT"].pop()

    result = engine.run({"sector_market_data": data})

    assert not result.ok
    assert "series lengths differ" in result.error
