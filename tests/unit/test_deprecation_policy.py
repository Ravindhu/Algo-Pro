"""A DeprecationWarning in OUR code must fail the build.

Why this exists: two deprecations accumulated unnoticed
(``DataFrame.sort_index(inplace=True)``, removed in pandas 4, and
``datetime.utcfromtimestamp()``, deprecated in Python 3.12+) because nothing
failed on them. They were found only by reading a warnings summary nobody reads.

The policy lives in ``pyproject.toml`` ``[tool.pytest.ini_options] filterwarnings``.
These tests keep it honest: they fail if it is removed, and they prove the scope
is right in BOTH directions - our code raises, third-party libraries do not.
"""
import warnings

import pytest

try:                                        # pytest 7.1+ / 8
    from _pytest.config.exceptions import PytestConfigWarning  # noqa: F401
except Exception:                           # pragma: no cover
    PytestConfigWarning = Warning


def _config_policy() -> list[str]:
    """The filterwarnings list from the root pyproject.toml."""
    import pathlib
    import tomllib
    root = pathlib.Path(__file__).resolve().parents[2]
    data = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    return list(
        data["tool"]["pytest"]["ini_options"].get("filterwarnings", []))


def test_the_policy_exists_and_errors_on_deprecation():
    policy = _config_policy()
    assert policy, "filterwarnings was removed from pyproject.toml"
    assert any(p.startswith("error::DeprecationWarning") for p in policy), (
        "a bare `error::DeprecationWarning` entry is required; got %r" % (policy,))


def test_third_party_modules_are_not_forced_to_raise():
    """A dependency's upgrade schedule is not ours to fix.

    Without these entries the suite would break whenever pandas/numpy/optuna
    shipped a deprecation - noise that trains people to ignore the policy.
    """
    policy = " ".join(_config_policy())
    for lib in ("pandas", "numpy"):
        assert f"ignore::DeprecationWarning:{lib}" in policy, (
            f"{lib} must be exempt; policy is {policy!r}")


def test_our_own_deprecated_call_would_fail():
    """Positive control: the filter really converts our warning into an error.

    ``warnings.warn_explicit`` attributes the warning to a module name we do NOT
    exempt, which is exactly the "our code" case.
    """
    with pytest.raises(DeprecationWarning):
        warnings.warn_explicit(
            "our own scheduled breakage",
            DeprecationWarning,
            filename="<ourcode>",
            lineno=1,
            module="quant.some_module",
            registry={},
        )


def test_an_exempt_module_is_tolerated():
    """Negative control: a third-party-attributed warning passes through."""
    warnings.warn_explicit(
        "a library's scheduled breakage",
        DeprecationWarning,
        filename="<thirdparty>",
        lineno=1,
        module="pandas.core.frame",
        registry={},
    )
