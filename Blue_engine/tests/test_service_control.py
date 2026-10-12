"""
test_service_control.py - the Service Control panel: probes + the START guard.

WHY THIS FILE EXISTS
--------------------
The panel's whole value is that its status is HONEST and its START button is
IDEMPOTENT. A status panel that lies (reports DOWN for a live service) is worse
than no panel, and a Start button that can double-launch is how you end up with
the duplicate-process mess this project already had to clean up twice.

These tests are pure: no launching, no killing, no network dependence on a
service actually being up (the "down" cases are asserted via a closed port).
"""
from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from blue_engine.dashboard.ui import service_control as sc  # noqa: E402


def _check(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)


# --------------------------------------------------------------------------- #
# 1. probes never raise and are honest about a CLOSED port
# --------------------------------------------------------------------------- #

def test_probe_tcp_false_on_a_closed_port():
    # Port 1 is never a listening service; the probe must say False, not raise.
    _check(sc.probe_tcp(1, timeout=0.3) is False,
           "a closed port must probe False")


def test_probe_http_false_on_a_closed_port():
    _check(sc.probe_http(1, timeout=0.5) is False,
           "a closed port must probe False over HTTP")


def test_probe_ws_false_on_a_closed_port():
    _check(sc.probe_ws(1, timeout=0.5) is False,
           "a closed port must probe False for the WS handshake")


def test_probes_return_bools_not_exceptions():
    """Every probe must be safe to call on any input, including junk ports."""
    for fn in (sc.probe_tcp, sc.probe_http, sc.probe_ws):
        for bad in (-1, 0, 99999, "not-a-port"):
            try:
                out = fn(bad, timeout=0.2)
            except Exception as exc:  # noqa: BLE001
                raise AssertionError(
                    f"{fn.__name__}({bad!r}) raised {exc!r} instead of "
                    f"returning a bool") from exc
            _check(isinstance(out, bool),
                   f"{fn.__name__}({bad!r}) returned {type(out).__name__}")


# --------------------------------------------------------------------------- #
# 2. probe_all shape (the UI's contract)
# --------------------------------------------------------------------------- #

def test_probe_all_returns_the_ui_contract(monkeypatch):
    # Stub the probes so the test asserts the CONTRACT, not whether services
    # happen to be running on this machine.
    monkeypatch.setattr(sc, "probe_http", lambda *a, **k: False)
    monkeypatch.setattr(sc, "probe_ws", lambda *a, **k: False)
    monkeypatch.setattr(sc, "openalgo_pids", lambda: [])
    snap = sc.probe_all()
    for key in ("services", "rest_up", "ws_up", "dash_up", "openalgo_up", "pids"):
        _check(key in snap, f"probe_all is missing {key!r}")
    _check(isinstance(snap["services"], list) and snap["services"],
           "services must be a non-empty list for the table to render")
    for row in snap["services"]:
        for col in ("service", "up", "endpoint", "detail"):
            _check(col in row, f"service row missing {col!r}: {row!r}")
        _check(isinstance(row["up"], bool),
               f"row 'up' must be a bool, got {type(row['up']).__name__}")


def test_probe_all_never_raises_with_services_down(monkeypatch):
    """The panel must render even when nothing is running."""
    monkeypatch.setattr(sc, "probe_http", lambda *a, **k: False)
    monkeypatch.setattr(sc, "probe_ws", lambda *a, **k: False)
    monkeypatch.setattr(sc, "openalgo_pids", lambda: [])
    snap = sc.probe_all()
    _check(isinstance(snap["pids"], list),
           "pids must always be a list, even when empty")
    _check(all(r["up"] is False for r in snap["services"][:3]),
           "with every probe down, the service rows must read DOWN")


def test_ws_probe_is_not_just_a_tcp_connect():
    """The WS proxy accepts TCP then demands an Upgrade.

    A bare connect therefore cannot prove health. Verified 2026-10-08: a plain
    socket probe reported the WS proxy DOWN while it was serving. This pins that
    we use the handshake (the functions must be distinct implementations).
    """
    _check(sc.probe_ws is not sc.probe_tcp,
           "probe_ws must not be an alias of the TCP probe")
    import inspect
    src = inspect.getsource(sc.probe_ws)
    _check("Sec-WebSocket-Key" in src and "101" in src,
           "probe_ws must perform a real upgrade handshake")


# --------------------------------------------------------------------------- #
# 3. the START guard - the safety-critical part
# --------------------------------------------------------------------------- #

def test_start_refuses_to_launch_when_openalgo_is_already_up(monkeypatch):
    """IDEMPOTENCE. This is what makes a Start button safe: a double click, a
    refresh, or two tabs cannot spawn a second OpenAlgo instance.

    NOTE on the mock: ``Popen`` and ``run`` are attributes of the SAME
    ``subprocess`` module object, so patching ``subprocess.Popen`` also
    intercepts ``subprocess.run`` -- which ``openalgo_pids()`` legitimately
    uses. We therefore patch the LAUNCHER itself (a distinct name on our own
    module) and assert counter-style that no launch request was made.
    """
    monkeypatch.setattr(sc, "probe_http", lambda *a, **k: True)
    monkeypatch.setattr(sc, "openalgo_pids", lambda: [1234])
    called = {"n": 0}

    def _boom(*a, **k):  # only reachable if the guard failed
        called["n"] += 1
        raise AssertionError("the launch path must not run when already up")

    monkeypatch.setattr(sc, "_launch_detached", _boom)
    res = sc.start_openalgo()
    _check(res["ok"] is True and res["already_running"] is True,
           f"must report already_running; got {res!r}")
    _check(res["launched"] is False,
           f"must NOT launch when already running; got {res!r}")
    _check(called["n"] == 0,
           "the launch path was reached despite the service being up")


def test_force_bypasses_the_guard_but_is_not_used_by_the_ui(monkeypatch, tmp_path):
    """``force=True`` exists for the CLI; the UI must never pass it."""
    import inspect
    cb = inspect.getsource(sc.register_callbacks)
    _check("force=True" not in cb and "force = True" not in cb,
           "the UI callback must never force a launch")

    # And force must actually skip the guard.
    monkeypatch.setattr(sc, "probe_http", lambda *a, **k: True)
    launcher = tmp_path / "launcher.py"
    launcher.write_text("pass\n", encoding="utf-8")
    monkeypatch.setattr(sc, "start_command_preview",
                        lambda: [sys.executable, str(launcher)])
    monkeypatch.setattr(sc, "_launch_detached", lambda *a, **k: 424242)
    res = sc.start_openalgo(force=True)
    _check(res["launched"] is True,
           f"force=True must launch despite the probe; got {res!r}")
    _check(res.get("pid") == 424242,
           f"the launched pid must be reported; got {res!r}")


def test_start_reports_clearly_when_the_launcher_is_missing(monkeypatch):
    monkeypatch.setattr(sc, "probe_http", lambda *a, **k: False)
    monkeypatch.setattr(sc, "start_command_preview",
                        lambda: [r"C:\nope\python.exe", r"C:\nope\start.py"])
    res = sc.start_openalgo()
    _check(res["ok"] is False and res["launched"] is False,
           f"a missing interpreter must not report success; got {res!r}")
    _check("not found" in res["reason"].lower(),
           f"the reason must say what is missing; got {res['reason']!r}")


def test_start_never_raises_even_if_popen_explodes(monkeypatch, tmp_path):
    """A callback must never 500 the operator."""
    monkeypatch.setattr(sc, "probe_http", lambda *a, **k: False)
    launcher = tmp_path / "launcher.py"
    launcher.write_text("pass\n", encoding="utf-8")
    monkeypatch.setattr(sc, "start_command_preview",
                        lambda: [sys.executable, str(launcher)])

    def _boom(*a, **k):
        raise OSError("simulated spawn failure")

    monkeypatch.setattr(sc, "_launch_detached", _boom)
    res = sc.start_openalgo()
    _check(res["ok"] is False,
           f"a failed spawn must report ok=False, not raise; got {res!r}")
    _check("launch failed" in res["reason"].lower(),
           f"the reason must explain the failure; got {res['reason']!r}")


def test_start_sets_app_mode_standalone(monkeypatch, tmp_path):
    """SOP section 2: APP_MODE=standalone stops app.py double-starting the WS
    proxy -- the exact cause of the orphan 8765 holders we cleaned up."""
    monkeypatch.setattr(sc, "probe_http", lambda *a, **k: False)
    launcher = tmp_path / "launcher.py"
    launcher.write_text("pass\n", encoding="utf-8")
    monkeypatch.setattr(sc, "start_command_preview",
                        lambda: [sys.executable, str(launcher)])
    seen = {}

    def _capture(argv, env=None, cwd=None):
        seen["argv"] = argv
        seen.update(env or {})
        return 1

    monkeypatch.setattr(sc, "_launch_detached", _capture)
    sc.start_openalgo()
    _check(seen.get("APP_MODE") == "standalone",
           f"APP_MODE must be standalone; got {seen.get('APP_MODE')!r}")
    _check(seen.get("argv"), f"the launcher must pass an argv; got {seen!r}")


def test_start_does_not_block_for_the_full_boot(monkeypatch):
    """The POST must return fast. Blocking ~40 s would freeze the browser and
    invite a reclick -- which is how duplicate processes appear."""
    _check(sc._LAUNCH_SETTLE_S <= 5,
           f"the launch settle time is too long: {sc._LAUNCH_SETTLE_S}s")


# --------------------------------------------------------------------------- #
# 4. the safety rationale is ENFORCED, not just documented
# --------------------------------------------------------------------------- #

def test_there_is_no_stop_function():
    """Stop is deliberately not implemented -- see the module docstring.

    If someone later adds ``stop_openalgo``, this test fails on purpose so the
    decision is revisited deliberately rather than by accident.
    """
    for name in dir(sc):
        _check("stop" not in name.lower(),
               f"a stop control appeared: {name!r}. Stop is deliberately not a "
               f"button (it can strand open positions); see the module docstring.")


def test_there_is_no_blue_engine_start():
    """The engine is a run-to-completion CLI: starting it means placing an
    order, which SOP section 4 forbids from the dashboard.

    We assert on the SOURCE, not ``__doc__``: ``__doc__`` on the imported
    module is the docstring object, whose repr escaping made the earlier check
    unreliable.
    """
    import inspect
    src = inspect.getsource(sc)
    _check("run-to-completion" in src,
           "the module must document why the engine has no button")
    # And the panel must SAY it, so the operator learns it from the UI.
    sentinel = {"services": [], "rest_up": True, "ws_up": True,
                "dash_up": True, "openalgo_up": True, "pids": []}
    _check("no Start button" in repr(sc.control_panel(sentinel)),
           "the panel must tell the operator the engine has no Start button")


# --------------------------------------------------------------------------- #
# 5. the panel renders from any snapshot (including all-down)
# --------------------------------------------------------------------------- #

def test_control_panel_renders_from_a_synthetic_snapshot(monkeypatch):
    monkeypatch.setattr(sc, "openalgo_pids", lambda: [1234])
    snap = {
        "services": [
            {"service": "OpenAlgo REST", "up": True,
             "endpoint": "http://127.0.0.1:5000/", "detail": "answering"},
            {"service": "OpenAlgo WS proxy", "up": False,
             "endpoint": "ws://127.0.0.1:8765", "detail": "no 101 upgrade"},
        ],
        "rest_up": True, "ws_up": False, "dash_up": True,
        "openalgo_up": True, "pids": [1234],
    }
    panel = sc.control_panel(snap)
    _check(panel is not None, "the panel must render")
    text = repr(panel)
    for needle in ("Service Control", "svc-start-openalgo", "svc-poll",
                   "Stop is deliberately not a button",
                   "no Start button"):
        _check(needle in text,
               f"the panel is missing {needle!r}")


def test_start_button_is_disabled_when_already_running():
    """The UI must not invite the operator to do the unsafe thing."""
    up = {"services": [], "rest_up": True, "ws_up": True, "dash_up": True,
          "openalgo_up": True, "pids": []}
    text = repr(sc.control_panel(up))
    _check("already running" in text.lower(),
           "the button must say it is already running when up")

    down = {"services": [], "rest_up": False, "ws_up": False, "dash_up": True,
            "openalgo_up": False, "pids": []}
    text2 = repr(sc.control_panel(down))
    _check("Start OpenAlgo" in text2,
           "the button must offer Start when down")
