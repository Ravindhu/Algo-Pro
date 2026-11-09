"""Upgrade pin: the fingerprint must depend on content only, never on where the
folder lives or which line endings the checkout happens to use."""
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


def test_crlf_checkout_matches_lf_pin(tmp_path):
    """A Windows clone carries CRLF; the vendored tree is LF. Same pin.

    Without the fold, every Windows host reports MISMATCH against a pin that
    was computed on Linux CI (2026-11-09, reproduced with a real clone).
    """
    uc = _module()
    lf = _make_folder(tmp_path / "lf", "flask==3\n")
    crlf = _make_folder(tmp_path / "crlf", "flask==3\n")
    # Rewrite EVERY watched file as explicit CRLF bytes. write_text() above used
    # the platform newline, so the fold is only proven if we normalise here.
    for rel in ("requirements.txt", "pyproject.toml", ".gitignore",
                "install/x.sh"):
        raw = (crlf / rel).read_bytes().replace(b"\r\n", b"\n")
        (crlf / rel).write_bytes(raw.replace(b"\n", b"\r\n"))
    assert (crlf / "requirements.txt").read_bytes() == b"flask==3\r\n"
    assert uc.fingerprint(lf)["_rolled"] == uc.fingerprint(crlf)["_rolled"]


def test_repo_clone_matches_pin_when_present():
    """The runtime clone openalgo/ must be the same version as the tracked copy.

    Skipped when the clone is absent: openalgo/ is git-ignored and built by the
    installer, so a fresh CI checkout legitimately has no clone.
    """
    import pytest

    uc = _module()
    clone = ROOT / "openalgo"
    if not (clone / "requirements.txt").is_file():
        pytest.skip("openalgo/ clone not present (installer creates it)")
    assert uc.fingerprint(clone)["_rolled"] == uc.read_pin()
