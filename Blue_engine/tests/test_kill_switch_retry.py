"""Kill-switch store retries transient sqlite locks (audit 2026-11-03, fix 5).

A brief lock must not disable the durable store for the whole process. A
persistent failure must still degrade, and corruption must not be retried.
"""
from __future__ import annotations

import sqlite3

import pytest

from blue_engine.core import kill_switch_store as ks


@pytest.fixture
def fresh_store(tmp_path, monkeypatch):
    monkeypatch.setenv("BLUE_KILL_SWITCH_DB_PATH", str(tmp_path / "ks.db"))
    monkeypatch.setattr(ks, "_CONN", None)
    monkeypatch.setattr(ks, "_DEGRADED", False)
    monkeypatch.setattr(ks, "_DEGRADE_DETAIL", "")
    monkeypatch.setattr(ks.time, "sleep", lambda _s: None)
    yield
    ks.close_store()


def test_transient_lock_is_retried_until_it_clears(fresh_store):
    attempts = {"n": 0}

    def op():
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise sqlite3.OperationalError("database is locked")
        return "ok"

    assert ks._with_retry(op, phase="write") == "ok"
    assert attempts["n"] == 3


def test_persistent_lock_raises_after_every_attempt(fresh_store):
    attempts = {"n": 0}

    def op():
        attempts["n"] += 1
        raise sqlite3.OperationalError("database is locked")

    with pytest.raises(sqlite3.OperationalError):
        ks._with_retry(op, phase="write")
    assert attempts["n"] == ks._RETRY_ATTEMPTS


def test_corruption_is_not_retried(fresh_store):
    attempts = {"n": 0}

    def op():
        attempts["n"] += 1
        raise sqlite3.DatabaseError("file is not a database")

    with pytest.raises(sqlite3.DatabaseError):
        ks._with_retry(op, phase="read")
    assert attempts["n"] == 1


def test_a_persistent_write_failure_still_fails_closed(fresh_store, monkeypatch):
    def always_locked(op, phase):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(ks, "_with_retry", always_locked)
    assert ks.save_state("drawdown breach", -0.07) is False
    assert ks.store_available() is False
    with pytest.raises(RuntimeError):
        ks.require_durable()
