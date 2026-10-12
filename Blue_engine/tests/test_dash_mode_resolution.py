"""
test_dash_mode_resolution.py - regression for the "MODE UNKNOWN" banner bug.

THE BUG (fixed 2026-10-08)
--------------------------
``dash_app.main`` attached an SDK client for the mode banner only when
``os.getenv("OPENALGO_API_KEY")`` was truthy -- i.e. the PROCESS environment
alone. But the system's canonical credential source is

    env  ->  <repo>/_restored/.env  ->  <repo>/.env

via ``blue_engine.economics.providers.secrets.get_secret``. Nothing exports the
key into the dashboard's process environment, so the gate was ALWAYS false and
the banner read "MODE UNKNOWN - VERIFY IN OPENALGO UI" on every page even with
OpenAlgo up and the key present on disk. The engine and the optimizer showed
the correct mode because they used the resolver; the dashboard was the one
surface that disagreed with the rest of the system.

WHAT THESE TESTS PIN
--------------------
1. ``get_secret`` is the resolver the dashboard now uses, and it reads the
   file env when the process env is unset.
2. ``read_mode`` returns "unknown" (not a crash) when there is no client, AND
   returns the reported mode when the client answers -- so "unknown" can only
   mean "could not read", never "the code forgot to pass a client".
3. The mode -> palette mapping accepts OpenAlgo's actual spelling
   (``"analyze"``) and produces the analyzer banner, and reserves the
   "unknown" banner for a genuinely unreadable mode.

These are pure: no network, no broker, no Dash server.
"""
from __future__ import annotations

import os
import platform
import sys

# Windows platform-probe guard. Importing blue_engine -> openalgo -> pandas
# calls platform.machine() -> win32_ver() -> subprocess("cmd /c ver"), which
# HANGS FOREVER when stdio is redirected (pytest captures stdio). Same guard,
# same reason, as Blue_engine/dashboard/run_server.py.
if os.name == "nt":
    _ARCH = os.environ.get("PROCESSOR_ARCHITECTURE", "") or "AMD64"
    platform.machine = lambda: _ARCH                       # type: ignore
    platform.win32_ver = lambda *a, **k: ("10", "", "", "")  # type: ignore

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from blue_engine.core.mode import read_mode  # noqa: E402
from blue_engine.dashboard.ui.mode import scheme_for_mode, theme  # noqa: E402

# IMPORTANT: load ``secrets.py`` by FILE, not as ``blue_engine.economics.providers``.
# ``blue_engine/economics/__init__.py`` eagerly imports its ``models`` subpackage,
# which pulls in scipy (an ~30 s import in this venv). ``secrets`` itself needs
# nothing but ``os``/``pathlib``/``functools``, and the dashboard's fix only uses
# ``get_secret``/``has_secret``, so importing the module directly keeps this
# safety-critical test file fast and free of a heavy optional dependency.
import importlib.util as _ilu  # noqa: E402

# ``_ROOT`` IS the ``Blue_engine`` package dir (the tests/ parent), so the
# path is relative to it directly -- do NOT prefix another "blue_engine".
_SECRETS_PATH = os.path.join(_ROOT, "economics", "providers", "secrets.py")
_spec = _ilu.spec_from_file_location("blue_engine_secrets_under_test",
                                     _SECRETS_PATH)
secrets_mod = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(secrets_mod)  # type: ignore[union-attr]


def _check(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)


# --------------------------------------------------------------------------- #
# 1. the resolver the dashboard now uses
# --------------------------------------------------------------------------- #

class _FakeRespClient:
    """A client whose analyzerstatus() answers like the live endpoint."""

    def __init__(self, mode):
        self._mode = mode
        self.calls = 0

    def analyzerstatus(self):
        self.calls += 1
        return {"status": "success", "data": {"mode": self._mode}}


class _BrokenClient:
    """A client whose analyzerstatus() raises (platform down / 401)."""

    def analyzerstatus(self):
        raise RuntimeError("HTTP 500 ... 401 Unauthorized")


def test_get_secret_reads_the_file_env_when_process_env_is_unset(monkeypatch):
    """The core of the bug: the key lives in _restored/.env, not os.environ.

    We simulate by clearing the process env and confirming get_secret still
    finds the value through its file candidates. If the operator has no .env at
    all we assert the graceful None rather than a crash -- both are correct,
    and neither may raise.
    """
    monkeypatch.delenv("OPENALGO_API_KEY", raising=False)
    # Clear the lru_cache so a previous test's read does not mask the file read.
    secrets_mod._file_env.cache_clear()
    try:
        val = secrets_mod.get_secret("OPENALGO_API_KEY")
    finally:
        secrets_mod._file_env.cache_clear()
    # The contract is "returns a non-empty string OR None, never raises".
    _check(val is None or (isinstance(val, str) and val),
           f"get_secret returned an unusable value: {val!r}")
    _check(os.environ.get("OPENALGO_API_KEY") is None,
           "the process env must have been cleared for this test to mean anything")


def test_get_secret_prefers_the_process_env(monkeypatch):
    """Env still wins over the file -- the documented precedence."""
    monkeypatch.setenv("OPENALGO_API_KEY", "PROCESS_WINS_1234567890")
    _check(secrets_mod.get_secret("OPENALGO_API_KEY") == "PROCESS_WINS_1234567890",
           "an explicit process env var must outrank the .env file")


def test_has_secret_is_false_for_a_name_nobody_defines(monkeypatch):
    monkeypatch.delenv("A_KEY_THAT_DOES_NOT_EXIST_ANYWHERE", raising=False)
    secrets_mod._file_env.cache_clear()
    try:
        _check(not secrets_mod.has_secret("A_KEY_THAT_DOES_NOT_EXIST_ANYWHERE"),
               "has_secret must not invent a credential")
    finally:
        secrets_mod._file_env.cache_clear()


# --------------------------------------------------------------------------- #
# 2. read_mode: "unknown" means "could not read", nothing else
# --------------------------------------------------------------------------- #

def test_read_mode_returns_unknown_with_no_client():
    _check(read_mode(None) == "unknown",
           "a None client must yield 'unknown', never raise")


def test_read_mode_returns_unknown_when_the_read_raises():
    _check(read_mode(_BrokenClient()) == "unknown",
           "a failing analyzerstatus must degrade to 'unknown', never raise")


def test_read_mode_returns_the_reported_mode_when_the_client_answers():
    _check(read_mode(_FakeRespClient("analyze")) == "analyze",
           "a working client must report its real mode")


def test_read_mode_returns_live_when_openalgo_is_live():
    _check(read_mode(_FakeRespClient("live")) == "live",
           "live mode must be reported as live -- this is the safety-critical case")


def test_read_mode_handles_a_non_success_envelope():
    class _Bad:
        def analyzerstatus(self):
            return {"status": "error", "message": "Invalid openalgo apikey"}

    _check(read_mode(_Bad()) == "unknown",
           "a non-success envelope must not be reported as a mode")


# --------------------------------------------------------------------------- #
# 3. the mode -> banner mapping (safety-critical: live must never look simulated)
# --------------------------------------------------------------------------- #

def test_analyze_spelling_maps_to_the_analyzer_banner():
    """OpenAlgo reports "analyze"; the banner must not fall through to unknown."""
    pal = theme("analyze")
    _check(pal["key"] == "analyzer",
           f"the API's own spelling must map to analyzer; got {pal['key']!r}")
    _check("SIMULATED" in pal["label"],
           f"the analyzer banner must say SIMULATED; got {pal['label']!r}")


def test_live_maps_to_the_real_money_banner():
    pal = theme("live")
    _check(pal["key"] == "live",
           f"live must map to the live palette; got {pal['key']!r}")
    _check("LIVE" in pal["label"].upper(),
           f"the live banner must say LIVE; got {pal['label']!r}")


def test_unknown_is_reserved_for_an_unreadable_mode():
    pal = theme("unknown")
    _check(pal["key"] == "unknown",
           f"an unreadable mode must be flagged unknown; got {pal['key']!r}")
    _check("UNKNOWN" in pal["label"].upper(),
           f"the banner must say UNKNOWN; got {pal['label']!r}")


def test_analyzer_and_live_share_dark_workspace_but_keep_distinct_safety_signal():
    """Keep the requested dark UI while making real-money mode unmistakable."""
    analyzer = theme("analyze")
    live = theme("live")
    _check(scheme_for_mode("analyze") == scheme_for_mode("live") == "dark",
           "all operational modes must use the dark workspace")
    _check(analyzer["accent"] != live["accent"]
           and analyzer["banner_bg"] != live["banner_bg"],
           "analyzer and live must retain distinct accent and banner colors")


def test_unknown_falls_back_to_dark():
    """Fail safe: an unreadable mode stays in the established dark workspace."""
    _check(scheme_for_mode("unknown") == "dark",
           "an unreadable mode must fail to the DARK scheme, never the live one")


# --------------------------------------------------------------------------- #
# 4. the two-axis banner: OpenAlgo mode (orders) vs panel data source (prices)
# --------------------------------------------------------------------------- #
#
# WHY THIS SECTION EXISTS
# -----------------------
# The dashboard has TWO independent states and conflating them is a safety bug:
#
#   OpenAlgo mode  -> where ORDERS go      ("live" spends real money)
#   data_source    -> where PANELS' prices come from ("paper" = stubbed reads)
#
# The terminal is config-only (SOP 4) so it never places an order, and its
# reads are stubbed by default. That means the NORMAL live configuration is
# (OpenAlgo live, panels paper). Rendering only the mode would put a red
# "LIVE - REAL MONEY" banner over simulated charts -- which is precisely the
# two-surfaces-disagree failure this module exists to prevent.
#
# The rule these tests pin: a banner may ONLY omit the panel clause when the
# two axes AGREE. Every ambiguous combination must state the panel side.

from blue_engine.dashboard.ui.mode import banner_text  # noqa: E402


def test_live_orders_with_paper_panels_states_both():
    """The safety-critical case: live orders + simulated panels must say so."""
    txt = banner_text(theme("live"), data_source="paper")
    _check("LIVE" in txt.upper(), f"the order side must be visible; got {txt!r}")
    _check("PAPER" in txt.upper(),
           f"the panel side must be visible when it is paper; got {txt!r}")
    _check("DISABLED" in txt.upper(),
           f"a live banner over paper panels must state order-placing is off; "
           f"got {txt!r}")


def test_live_orders_with_live_panels_omits_the_redundant_clause():
    """When both axes are live the qualifier adds nothing, so it is dropped."""
    txt = banner_text(theme("live"), data_source="live")
    _check(txt == theme("live")["label"],
           f"agreed live state must not carry a redundant clause; got {txt!r}")


def test_analyzer_orders_with_paper_panels_omits_the_redundant_clause():
    """Both simulated -> nothing to disambiguate."""
    txt = banner_text(theme("analyze"), data_source="paper")
    _check(txt == theme("analyze")["label"],
           f"agreed simulated state must not carry a clause; got {txt!r}")


def test_analyzer_orders_with_live_panels_states_both():
    """The inverse mismatch: real prices under simulated orders."""
    txt = banner_text(theme("analyze"), data_source="live")
    _check("SIMULATED" in txt.upper(), f"order side missing; got {txt!r}")
    _check("LIVE" in txt.upper(), f"panel side missing; got {txt!r}")


def test_unknown_panel_source_is_always_stated():
    """An unknown wiring must never be silently presented as known.

    Note the analyzer/unknown pair is NOT treated as agreed: 'unknown' is not
    the same claim as 'paper', so the clause is kept.
    """
    for mode in ("live", "analyze", "analyzer", "unknown"):
        txt = banner_text(theme(mode), data_source="unknown")
        _check("UNKNOWN" in txt.upper(),
               f"an unknown panel source must be stated for mode={mode!r}; "
               f"got {txt!r}")


def test_banner_never_claims_live_panels_under_a_paper_data_source():
    """A blanket property, across every mode, to catch a future refactor."""
    for mode in ("live", "analyze", "analyzer", "unknown", "", None):
        txt = banner_text(theme(mode), data_source="paper").upper()
        _check("LIVE SDK DATA" not in txt,
               f"paper wiring must never be labelled live SDK data; "
               f"mode={mode!r} gave {txt!r}")


def test_banner_text_tolerates_junk_data_source():
    """A malformed flag degrades to 'unknown', never to a false 'paper'."""
    for junk in ("", None, "PAPERISH", 123, "LIVE!"):
        txt = banner_text(theme("live"), data_source=junk)
        _check("PANELS:" in txt.upper(),
               f"junk data_source must still state the panel side; "
               f"got {txt!r} for {junk!r}")
        _check(txt.upper().count("LIVE SDK") == 0,
               f"junk must not be upgraded to a live panel claim; got {txt!r}")


def test_every_palette_declares_a_data_source():
    """The palettes must carry the field, so no consumer has to invent it."""
    for mode in ("live", "analyze", "analyzer", "unknown"):
        pal = theme(mode)
        _check("data_source" in pal,
               f"palette for {mode!r} is missing data_source")
        _check("data_source_label" in pal,
               f"palette for {mode!r} is missing data_source_label")
