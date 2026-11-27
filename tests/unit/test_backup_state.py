"""Tests for scripts/ops/backup_state.py: setup, backup, retention and restore (temp dirs only)."""
import importlib.util
import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "ops" / "backup_state.py"
spec = importlib.util.spec_from_file_location("backup_state", SCRIPT)
bs = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bs)


def _make_db(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.execute("create table t (v text)")
    con.execute("insert into t values (?)", (value,))
    con.commit()
    con.close()


@pytest.fixture
def root(tmp_path):
    r = tmp_path / "repo"
    (r / "_restored").mkdir(parents=True)
    (r / "_restored" / ".env").write_text("FERNET_SALT='x'\n", encoding="utf-8")
    (r / "data").mkdir()
    (r / "data" / "schedule_settings.json").write_text("{}", encoding="utf-8")
    tree = r / "src" / "quant" / "openalgo_platform"
    _make_db(tree / "db" / "openalgo.db", "live-row")
    (tree / "keys").mkdir(parents=True)
    (tree / "keys" / "k.pem").write_text("key", encoding="utf-8")
    return r


def test_setup_creates_layout_and_never_creates_env(tmp_path):
    r = tmp_path / "fresh"
    r.mkdir()
    report = bs.setup(r)
    for sub in ("_restored/db", "_restored/keys", "_restored/download", "data"):
        assert (r / sub).is_dir()
    assert (r / "_restored" / "README.md").exists()
    assert (r / "data" / "README.md").exists()
    assert not (r / "_restored" / ".env").exists()
    assert report["env_present"] is False


def test_setup_is_idempotent_and_keeps_existing_env(root):
    before = (root / "_restored" / ".env").read_text(encoding="utf-8")
    bs.setup(root)
    bs.setup(root)
    assert (root / "_restored" / ".env").read_text(encoding="utf-8") == before


def test_backup_copies_state_and_live_db_consistently(root, tmp_path):
    dest = tmp_path / "dest"
    target = bs.backup(root, dest, now=datetime(2026, 11, 27, 15, 45, 0))
    assert target.name == "20261127-154500"
    assert (target / "_restored" / ".env").exists()
    assert (target / "data" / "schedule_settings.json").exists()
    assert (target / "openalgo_live_keys" / "k.pem").exists()
    con = sqlite3.connect(target / "openalgo_live_db" / "openalgo.db")
    assert con.execute("select v from t").fetchone() == ("live-row",)
    con.close()
    manifest = json.loads((target / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["counts"]["openalgo_live_db"] == 1
    assert "FERNET_SALT" not in json.dumps(manifest)  # manifest holds names only


def test_retention_keeps_last_30_dated_folders_only(root, tmp_path):
    dest = tmp_path / "dest"
    dest.mkdir()
    start = datetime(2026, 1, 1, 15, 45)
    for i in range(35):
        (dest / (start + timedelta(days=i)).strftime("%Y%m%d-%H%M%S")).mkdir()
    (dest / "notes-keep-me").mkdir()  # not a backup folder: never deleted
    bs.backup(root, dest, keep=30, now=datetime(2026, 3, 1, 15, 45))
    dated = sorted(p.name for p in dest.iterdir() if bs.STAMP_RE.match(p.name))
    assert len(dated) == 30
    assert (dest / "notes-keep-me").exists()
    assert "20260101-154500" not in dated  # oldest removed


def test_restore_refuses_while_stack_runs(root, tmp_path):
    src = bs.backup(root, tmp_path / "dest", now=datetime(2026, 11, 27, 15, 45))
    with pytest.raises(SystemExit):
        bs.restore(root, src, tmp_path / "dest", port_check=lambda port: port == 5000)


def test_restore_round_trip_and_saves_current_state(root, tmp_path):
    dest = tmp_path / "dest"
    src = bs.backup(root, dest, now=datetime(2026, 11, 27, 15, 45))
    (root / "_restored" / ".env").write_text("CHANGED=1\n", encoding="utf-8")
    safety = bs.restore(root, src, dest, port_check=lambda port: False)
    assert (root / "_restored" / ".env").read_text(encoding="utf-8") == "FERNET_SALT='x'\n"
    assert safety.name.startswith("pre-restore-")
    assert (safety / "_restored" / ".env").read_text(encoding="utf-8") == "CHANGED=1\n"
