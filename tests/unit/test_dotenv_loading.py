"""Local .env loader and API key read (decoupling stage 4, item 13). No Blue_engine needed.

Flow stage: OpenAlgo (secret loading for the order client)
Status: PHASE 1 IMPLEMENTED
Blue's loader had the same rules: no override of existing variables, missing file is not an error.
"""
import ast
import pathlib

import pytest

from quant.core import env as env_mod
from quant.execution.openalgo_client import OpenAlgoClient

SRC = pathlib.Path(__file__).resolve().parents[2] / "src" / "quant"


def _clean(monkeypatch, name):
    """Make the variable absent now and restore the original state after the test."""
    monkeypatch.setenv(name, "x")
    monkeypatch.delenv(name)


def _write(tmp_path, text):
    path = tmp_path / ".env"
    path.write_text(text, encoding="utf-8")
    return path


def test_missing_file_returns_false_and_changes_nothing(tmp_path, monkeypatch):
    _clean(monkeypatch, "QT_ENV_A")
    assert env_mod.load_env_file(tmp_path / "nope.env") is False
    import os
    assert "QT_ENV_A" not in os.environ


def test_loads_plain_key_value(tmp_path, monkeypatch):
    import os
    _clean(monkeypatch, "QT_ENV_B")
    assert env_mod.load_env_file(_write(tmp_path, "QT_ENV_B=hello\n")) is True
    assert os.environ["QT_ENV_B"] == "hello"


def test_existing_process_variable_wins(tmp_path, monkeypatch):
    import os
    monkeypatch.setenv("QT_ENV_C", "from-shell")
    env_mod.load_env_file(_write(tmp_path, "QT_ENV_C=from-file\n"))
    assert os.environ["QT_ENV_C"] == "from-shell"


def test_comments_and_blank_lines_are_ignored(tmp_path, monkeypatch):
    import os
    _clean(monkeypatch, "QT_ENV_D")
    env_mod.load_env_file(_write(tmp_path, "# a comment\n\nQT_ENV_D=ok\n# another\n"))
    assert os.environ["QT_ENV_D"] == "ok"


def test_quotes_are_removed(tmp_path, monkeypatch):
    import os
    _clean(monkeypatch, "QT_ENV_E")
    _clean(monkeypatch, "QT_ENV_F")
    env_mod.load_env_file(_write(tmp_path, 'QT_ENV_E="two words"\nQT_ENV_F=\'single\'\n'))
    assert os.environ["QT_ENV_E"] == "two words"
    assert os.environ["QT_ENV_F"] == "single"


def test_export_prefix_is_accepted(tmp_path, monkeypatch):
    import os
    _clean(monkeypatch, "QT_ENV_G")
    env_mod.load_env_file(_write(tmp_path, "export QT_ENV_G=exported\n"))
    assert os.environ["QT_ENV_G"] == "exported"


def test_api_key_is_read_from_the_loaded_file(tmp_path, monkeypatch):
    _clean(monkeypatch, "OPENALGO_API_KEY")
    monkeypatch.setattr(env_mod, "DEFAULT_ENV_FILE", _write(tmp_path, "OPENALGO_API_KEY=key-abc\n"))
    assert OpenAlgoClient.api_key_from_env() == "key-abc"


def test_missing_key_fails_closed(tmp_path, monkeypatch):
    _clean(monkeypatch, "OPENALGO_API_KEY")
    monkeypatch.setattr(env_mod, "DEFAULT_ENV_FILE", tmp_path / "none.env")
    with pytest.raises(RuntimeError, match="OPENALGO_API_KEY is not set"):
        OpenAlgoClient.api_key_from_env()


def test_database_key_discovery_is_never_used(tmp_path, monkeypatch):
    """Blue's resolve_api_key can decrypt a key from the OpenAlgo DB. The quant path must not."""
    _clean(monkeypatch, "OPENALGO_API_KEY")
    monkeypatch.setattr(env_mod, "DEFAULT_ENV_FILE", tmp_path / "none.env")
    with pytest.raises(RuntimeError):
        OpenAlgoClient.api_key_from_env()


@pytest.mark.parametrize("rel", ["core/env.py", "execution/openalgo_client.py"])
def test_key_path_has_no_blue_import(rel):
    tree = ast.parse((SRC / rel).read_text(encoding="utf-8"))
    names = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            names.append(node.module or "")
    assert not [n for n in names if "blue" in n.lower()], rel
