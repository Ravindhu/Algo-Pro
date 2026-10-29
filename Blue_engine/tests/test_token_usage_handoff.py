"""Tests for the token telemetry handoff (pipeline -> dashboard).

The governor cannot reach a separate dashboard process, so the run publishes a
snapshot. Rules under test:
  * a malformed snapshot is refused, never written;
  * no published run => the page reads {} (and says "not measured");
  * a publish/write failure must never raise into a trading run.
"""
from __future__ import annotations

import json

from blue_engine.dashboard import state


def _snapshot(calls: int = 4) -> dict:
    return {"enabled": True, "max_prompt_tokens": 3072, "max_tokens_out": 600,
            "chain": ["anthropic", "ollama"],
            "backends": [{"backend": "ollama", "configured": True}],
            "cache": {"hits": 1, "misses": 3, "hit_rate": 0.25},
            "meter": {"calls": calls, "cache_hits": 1,
                      "prompt_tokens_saved": 800,
                      "prompt_tokens_estimated": 2400,
                      "completion_tokens_reported": 450,
                      "compaction_events": 2, "by_backend": {"ollama": calls}}}


def test_publish_then_read_round_trips(tmp_path, monkeypatch):
    monkeypatch.setattr(state, "LAST_TOKENS", {})
    target = tmp_path / "tokens.json"

    published = state.publish_token_usage(_snapshot(), path=target)
    assert published["ok"] is True

    monkeypatch.setattr(state, "LAST_TOKENS", {})  # force the file path
    loaded = state.read_token_usage(path=target)
    assert loaded["meter"]["calls"] == 4
    assert loaded["chain"] == ["anthropic", "ollama"]
    assert json.loads(target.read_text(encoding="utf-8"))["meter"]["calls"] == 4


def test_malformed_snapshot_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(state, "LAST_TOKENS", {})
    target = tmp_path / "tokens.json"
    assert state.publish_token_usage({"nope": 1}, path=target)["ok"] is False
    assert not target.exists()


def test_no_run_means_empty_not_zero(tmp_path, monkeypatch):
    monkeypatch.setattr(state, "LAST_TOKENS", {})
    assert state.read_token_usage(path=tmp_path / "missing.json") == {}


def test_corrupt_file_reads_as_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(state, "LAST_TOKENS", {})
    target = tmp_path / "tokens.json"
    target.write_text("{not json", encoding="utf-8")
    assert state.read_token_usage(path=target) == {}


def test_write_failure_is_reported_not_raised(tmp_path, monkeypatch):
    monkeypatch.setattr(state, "LAST_TOKENS", {})
    # A directory cannot be opened as a file -> OSError inside the writer.
    result = state.publish_token_usage(_snapshot(), path=tmp_path)
    assert result["ok"] is False
    assert "reason" in result
