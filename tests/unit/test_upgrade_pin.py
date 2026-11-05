"""Upgrade pin: the fingerprint must depend on content only, never on where the folder lives."""
import importlib.util
import pathlib
import shutil

ROOT = pathlib.Path(__file__).resolve().parents[2]


def _module():
    spec = importlib.util.spec_from_file_location("upgrade_check", ROOT / "scripts/verify/upgrade_check.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _make_folder(base: pathlib.Path, text: str) -> pathlib.Path:
    d = base / "openalgo"
    (d / "install").mkdir(parents=True)
    (d / "requirements.txt").write_text(text, encoding="utf-8")
    (d / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    (d / ".gitignore").write_text("*.pyc\n", encoding="utf-8")
    (d / "install" / "x.sh").write_text("echo hi\n", encoding="utf-8")
    return d


def test_same_content_different_path_same_pin(tmp_path):
    uc = _module()
    a = _make_folder(tmp_path / "one", "flask==3\n")
    b = _make_folder(tmp_path / "two" / "deeper", "flask==3\n")
    assert uc.fingerprint(a)["_rolled"] == uc.fingerprint(b)["_rolled"]


def test_changed_content_changes_pin(tmp_path):
    uc = _module()
    a = _make_folder(tmp_path / "one", "flask==3\n")
    b = _make_folder(tmp_path / "two", "flask==4\n")
    assert uc.fingerprint(a)["_rolled"] != uc.fingerprint(b)["_rolled"]


def test_repo_pin_matches_vendored_copy():
    uc = _module()
    vendored = ROOT / "src/quant/openalgo_platform"
    pin = uc.read_pin()
    assert pin == uc.fingerprint(vendored)["_rolled"]
