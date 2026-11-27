"""One screen refresh: scrape the saved screen, enrich quotes, write the bot's screen file.

Flow stage: Liquidity screener -> screen file
Status: PHASE 3 IMPLEMENTED (read-only; no order path; runs inside the bot's loop every 5 minutes).

Two inputs, in order (2026-11-26):
  1. ``last_screen.json`` - WHICH names the Screener.in saved screen selected. Refreshed from
     screener.in only when it is missing or older than ``scrape_every_minutes`` (default 60).
     The saved screen changes at most daily, so re-scraping every 5 minutes would hit a site
     we do not own for no new information.
  2. ``latest.json`` - the bot's screen. Rebuilt EVERY cycle from the current quote band and
     the current ASM/GSM flag, because those DO change during the day.

Why parallel, and why no cache on the quotes: each refresh asks the broker for every candidate's
quote. The quote also carries the ASM/GSM surveillance flag, which can change during the day, so
a cached flag could let a stock placed under surveillance keep trading. Every run therefore
re-fetches. Parallel workers keep a run to seconds-to-a-minute for a few hundred symbols.

The Screener.in session is established ONCE (2026-11-26): the login is attempted only when the
scrape actually runs and only when no usable session exists. A successful login is persisted to
.env by ``screener_session`` and a same-day success is remembered in a marker file, so the desk
does not log in to screener.in every time the bot starts.

Exit codes (same meaning as scripts/ops/refresh_screen.py):
    0  wrote at least one tradable symbol
    1  band source unreachable (every quote failed for a transport reason)
    2  wrote the file but nothing passed the band filter
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from quant.data import screener_engine
from quant.data.circuit_band import enrich_candidates
from quant.data.bse_symbol import split_for_screen

DEFAULT_WORKERS = 4   # parallel quote requests; keep low unless the broker allows more


def _enrich_one(row: Dict[str, Any], quote_fn: Callable[[str, str], Any], exchange: str) -> Dict[str, Any]:
    result = enrich_candidates([row], lambda s, _e: quote_fn(s, exchange), exchange=exchange)
    out = result["candidates"][0]
    out["_failed"] = bool(result["failed"])
    return out


def enrich_parallel(rows: List[Dict[str, Any]], quote_fn: Callable[[str, str], Any], exchange: str,
                    workers: int = DEFAULT_WORKERS) -> List[Dict[str, Any]]:
    """Enrich every row with its band and flag. Returns rows in the same order. Never raises for one row."""
    if not rows:
        return []
    with ThreadPoolExecutor(max_workers=max(1, int(workers))) as pool:
        return list(pool.map(lambda r: _enrich_one(r, quote_fn, exchange), rows))


def refresh_once(candidates: List[Dict[str, Any]], quote_fn: Callable[[str, str], Any], target,
                 *, lookup_bse: Optional[Callable[[str], Optional[str]]] = None,
                 bse_quote_fn: Optional[Callable[[str, str], Any]] = None,
                 workers: int = DEFAULT_WORKERS, as_of: Any = None) -> Dict[str, Any]:
    """Enrich, screen and write the screen file atomically. Returns a summary dict (no exceptions for data).

    ``as_of`` becomes the file's ``as_of`` stamp and MUST be the clock the bot will judge it with.
    ``make_screen_refresher`` passes its own ``now``, so the two cannot drift.
    """
    nse_list, bse_list, unresolved = split_for_screen(candidates, lookup_bse or (lambda code: None))
    nse_rows = enrich_parallel(nse_list, quote_fn, "NSE", workers)
    bse_rows = enrich_parallel(bse_list, bse_quote_fn or quote_fn, "BSE", workers) if bse_list else []
    all_rows = nse_rows + bse_rows

    failed = [r for r in all_rows if r.pop("_failed", False)]
    transport = [r for r in failed if "quote" in str(r.get("band_reason") or "")]
    payload = {"candidates": all_rows, "symbols": [r["symbol"] for r in all_rows]}
    doc = screener_engine.screen_rows(payload, as_of=as_of, max_symbols=len(all_rows) or 1)
    # Carry each row's sector into the written rows. ``screen_rows`` builds a fixed 4-key row, so
    # the sector is attached here from the input row (the saved screen's own column).
    # ``risk_limits.sector_map`` reads exactly this key, and a missing sector leaves every name in
    # one capped UNKNOWN bucket - sector limits would silently stop separating anything.
    _attach_sectors(doc["results"], all_rows)
    wrote = _write_doc(target, doc)
    unreachable = bool(failed) and len(transport) == len(all_rows)
    code = 0 if wrote > 0 else (1 if unreachable else 2)
    return {
        "candidates": len(candidates),
        "enriched": len(all_rows) - len(failed),
        "selected": doc["counts"]["selected"],
        "dropped": doc["counts"]["dropped"],
        "wrote": wrote,
        "unresolved": unresolved,
        "exit_code": code,
    }


def _write_doc(target, doc: Dict[str, Any]) -> int:
    """Write a finished screen doc atomically. Returns the row count. One writer, one shape."""
    import json as _json
    import os
    import tempfile

    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(target.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            _json.dump(doc, fh, indent=1)
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return len(doc.get("results") or [])


def _attach_sectors(rows: List[Dict[str, Any]], source_rows: List[Dict[str, Any]]) -> None:
    """Copy ``sector`` from each input row onto the written row. Absent or blank stays absent."""
    by_symbol = {str(r.get("symbol") or "").strip().upper(): str(r.get("sector") or "").strip()
                 for r in source_rows}
    for row in rows:
        sector = by_symbol.get(str(row.get("symbol") or "").strip().upper(), "")
        if sector:
            row["sector"] = sector


def load_candidates(path, symbols: Optional[str] = None) -> List[Dict[str, Any]]:
    """Explicit symbols win; otherwise the selected rows of the saved Screener.in screen. Never raises.

    Accepts BOTH real shapes:
      * the engine's own ``screen_now()`` output - ``{ok, data: {results: [...]}}`` (what
        ``make_screener_scraper`` writes), and
      * a flat ``{results: [...]}`` file.
    Before 2026-11-26 only the flat shape was read, so the file the scraper wrote produced ZERO
    candidates: ``latest.json`` was never rebuilt and the bot traded nothing while every log line
    looked healthy.
    """
    import json

    if symbols:
        return [{"symbol": s.strip().upper(), "selected": True} for s in symbols.split(",") if s.strip()]
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return []
    if not isinstance(payload, dict):
        return []
    rows = payload.get("results")
    if rows is None and isinstance(payload.get("data"), dict):
        rows = payload["data"].get("results")
    return [dict(r) for r in (rows or []) if isinstance(r, dict) and r.get("selected")]


def make_screen_refresher(reader, api_key: str, *, source, target,
                          scrape: Optional[Callable[[], Dict[str, Any]]] = None,
                          scrape_every_minutes: int = 60) -> Callable[[Any], Dict[str, Any]]:
    """The callable the bot's loop runs every 5 minutes. Read-only: quotes and the symbol master only.

    Args:
        reader, api_key: the read-only OpenAlgo quote source.
        source: ``last_screen.json`` - the Screener.in selection.
        target: ``latest.json`` - the bot's screen file.
        scrape: optional callable that refreshes ``source`` from screener.in. Called only when
            ``source`` is missing or older than ``scrape_every_minutes``, so the bot does not
            re-scrape a site we do not own on every 5-minute cycle.
        scrape_every_minutes: age after which ``source`` is re-scraped. 0 means every cycle.

    The scrape never blocks the cycle: a failure is reported in the result and the bot carries
    on with whatever ``source`` already holds. A missing ``source`` after a failed scrape is
    ``exit_code=2`` (no candidates), which the bot already treats as "no new symbols".
    """
    from quant.data.bse_symbol import openalgo_bse_lookup

    def _stale(now) -> bool:
        """True when ``source`` is missing or old enough to re-scrape."""
        return source_is_stale(source, now, scrape_every_minutes)

    def refresh(now) -> Dict[str, Any]:
        scrape_note = None
        if scrape is not None and _stale(now):
            try:
                scrape_note = scrape()
            except Exception as exc:  # noqa: BLE001 - a scrape problem must not stop the bot
                scrape_note = {"ok": False, "reason": f"scrape raised {type(exc).__name__}"}
        candidates = load_candidates(source)
        if not candidates:
            return {"exit_code": 2, "reason": f"no candidates in {source}", "scrape": scrape_note}
        out = refresh_once(candidates, lambda s, e: reader.quotes(api_key, s, e), target,
                           lookup_bse=openalgo_bse_lookup, as_of=now)
        out["scrape"] = scrape_note
        return out
    return refresh


def make_screener_scraper(*, source, env_file=None,
                          run_engine: Optional[Callable[[], Dict[str, Any]]] = None
                          ) -> Callable[[], Dict[str, Any]]:
    """Build the callable that refreshes ``last_screen.json`` from screener.in.

    Two jobs, in this order:
      1. Establish a Screener.in session ONCE. A same-day success is remembered in a marker
         file, so the login is not repeated on every bot start (owner request 2026-11-26).
      2. Run the saved screen and write the selection to ``source``.

    Args:
        source: where ``last_screen.json`` is written (``data/screened/``).
        env_file: the .env holding the session cookie. Defaults to the repository root.
        run_engine: test seam. Returns a screener payload; defaults to the real engine.

    Returns:
        A zero-argument callable returning ``{ok, reason, count, session}``. Never raises.
    """
    target = Path(source)

    def scrape() -> Dict[str, Any]:
        result: Dict[str, Any] = {"ok": False, "reason": "", "count": 0, "session": "reused"}

        # ---- 1. session, once ------------------------------------------------------- #
        session_note = ensure_session(env_file=env_file)
        result["session"] = session_note.get("action", "unknown")
        if not session_note.get("usable", False):
            result["reason"] = f"screener session unusable: {session_note.get('reason', '')}"
            return result

        # ---- 2. the saved screen ---------------------------------------------------- #
        try:
            payload = (run_engine or _run_screener_engine)()
        except Exception as exc:  # noqa: BLE001 - report, never crash the loop
            result["reason"] = f"screener engine raised {type(exc).__name__}"
            return result
        if not isinstance(payload, dict) or not payload.get("ok", False):
            reason = (payload or {}).get("error") if isinstance(payload, dict) else None
            result["reason"] = f"screener run failed: {reason or 'no reason given'}"
            return result

        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        selected = data.get("selected") or data.get("symbols") or []
        result["count"] = len(selected) if isinstance(selected, list) else 0
        warnings = payload.get("warnings") or []
        if not (data.get("results") or []) and warnings:
            # Audit 2026-11-27: the engine returns ok=True with an EMPTY payload when the saved screen
            # cannot be read (for example URL_SCREENER_DOT_IN unset). Writing that would overwrite the
            # last good selection with nothing. Refuse and keep the file as it is.
            result["reason"] = f"screener returned no rows: {str(warnings[0])[:160]}"
            return result
        try:
            _write_last_screen(target, payload)
        except OSError as exc:
            result["reason"] = f"could not write {target.name}: {type(exc).__name__}"
            return result
        result["ok"] = True
        result["reason"] = f"{result['count']} selected"
        return result

    return scrape


def _run_screener_engine() -> Dict[str, Any]:
    """Run the saved screen once through the Blue engine. Returns its ``{ok, data, ...}`` payload.

    The engine's own config file (``input/screener_engine.json``) is loaded first, because
    ``BaseEngine.__init__`` only copies the dict it is handed - it does NOT read the file. Passing
    ``{"enrich": False}`` alone (the old code) therefore threw away ``screen_id``, ``screen_slug``,
    the screen rules and the transport limits, so the scrape could not even name a screen. Only the
    two enrichment keys are overridden on top of the file.
    """
    import json as _json

    from quant.core.blue_bridge import BlueEngineUnavailable, load

    try:
        module = load("screener.screener_engine")
    except BlueEngineUnavailable as exc:  # pragma: no cover - environment dependent
        raise RuntimeError(f"screener engine unavailable: {exc}") from exc

    cfg_path = (Path(__file__).resolve().parents[2] / "quant" / "engines" / "input"
                / "screener_engine.json")
    cfg: Dict[str, Any] = {}
    if cfg_path.is_file():
        try:
            cfg = (_json.loads(cfg_path.read_text(encoding="utf-8")).get("screener") or {})
        except (OSError, ValueError):
            cfg = {}

    # Enrichment fetches a company page per name with a 1.5 s gap - minutes for a full
    # universe. The BOT only needs WHICH names were selected, and the live prices come from
    # OpenAlgo quotes, so enrichment is off on the refresh path.
    cfg["enrich"] = False
    cfg["annual_report_enrichment"] = False
    engine = module.ScreenerEngine(config=cfg)
    payload = engine.screen_now()
    return payload if isinstance(payload, dict) else {"ok": False, "error": "engine returned no payload"}


def _write_last_screen(target: Path, payload: Dict[str, Any]) -> None:
    """Write the screener payload to ``last_screen.json`` atomically (temp file + os.replace)."""
    import json as _json
    import os
    import tempfile
    from datetime import datetime, timezone

    from quant.core.time_ist import naive_ist

    # as_of = when this selection was scraped, on the bot's IST clock. The dashboard orders its
    # screen files by this stamp, so a fresh scrape is shown even when the bot's file is older.
    stamped = dict(payload)
    stamped["as_of"] = naive_ist(datetime.now(timezone.utc)).isoformat()
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(target.parent), prefix="." + target.name + "-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            _json.dump(stamped, fh, indent=1, default=str)
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def ensure_session(*, env_file=None) -> Dict[str, Any]:
    """Make sure a Screener.in session exists, logging in AT MOST once per trading day.

    Order of preference (owner request 2026-11-26: refresh once, then reuse the id):
      1. A login already succeeded today AND a cookie is available -> ``action='reused'``.
         No network call at all.
      2. A cookie already in the environment -> ``action='cookie present'``. No network call.
      3. Otherwise sign in once via ``screener_session.refresh_session``. A success writes
         the cookie to .env AND the day marker, so the rest of the day is case 1.

    The marker logic lives in ``screener_session`` so the dashboard button and this scraper
    cannot disagree about what "already signed in today" means. Every value read here comes
    from the repository .env (owner rule 2026-11-26).

    Returns:
        ``{usable, action, reason}``. ``usable`` is False only when there is no cookie and
        the login failed, which is the one case the scraper cannot proceed.
    """
    from quant.core.env import DEFAULT_ENV_FILE
    from quant.data import screener_session as ss

    # Read .env FIRST. Step 2 below looks at os.environ, so without this a bot started from
    # a shell that never sourced .env would miss a perfectly good saved cookie.
    env_path = ss._load_env(env_file if env_file is not None else DEFAULT_ENV_FILE)

    if ss._saved_today(env_path):
        return {"usable": True, "action": "reused", "reason": "already signed in today"}

    import os as _os
    if (_os.environ.get(ss.SESSION_KEY) or "").strip():
        ss.mark_signed_in_today(env_path)
        return {"usable": True, "action": "cookie present", "reason": "cookie already set"}

    # reuse_today=False: the two checks above already handled the reuse cases, so a login
    # that still reaches here is the ONE attempt for the day.
    outcome = ss.refresh_session(env_file=env_path, reuse_today=False)
    if outcome.get("saved"):
        return {"usable": True, "action": "logged in", "reason": "session saved to .env"}
    if outcome.get("ok"):
        # The login worked but .env could not be written. The cookie IS live in this process,
        # so the scrape can proceed; it just will not survive a restart.
        return {"usable": True, "action": "logged in (not persisted)",
                "reason": outcome.get("reason", "")}
    return {"usable": False, "action": "failed", "reason": outcome.get("reason", "login failed")}


def source_is_stale(source, now, every_minutes: int) -> bool:
    """True when the saved selection is missing, unreadable or older than ``every_minutes``.

    Shared by the bot's refresher and the dashboard sign-in job, so both judge the file the same way.
    Cannot judge the age -> stale: a wrong "fresh" would freeze the universe.
    """
    try:
        path = Path(source)
        if not path.is_file():
            return True
        if every_minutes <= 0:
            return True
        age_s = float(now.timestamp()) - path.stat().st_mtime
        return age_s >= every_minutes * 60
    except (OSError, AttributeError, TypeError):
        return True


def owner_signin_job(actor: str, audit_path, source, *, every_minutes: int = 60,
                     session_runner: Optional[Callable[[], Dict[str, Any]]] = None,
                     scrape: Optional[Callable[[], Dict[str, Any]]] = None) -> Dict[str, Any]:
    """Dashboard OWNER sign-in, one background job (2026-11-27).

    1. Refresh the Screener.in session (once a day; reuse otherwise).
    2. If the saved selection (``last_screen.json``) is missing or older than ``every_minutes``,
       scrape the saved screen once, so the universe is ready before the bot starts.
    After this the bot refreshes its own screen every 5 minutes and re-scrapes when stale.

    Never raises. Every step is audited with the actor; no cookie value is written.
    ``session_runner`` and ``scrape`` are test seams.
    """
    from datetime import datetime, timezone

    from quant.audit.log import AuditLog
    from quant.data.screener_session import screener_login_after_signin

    audit = AuditLog(audit_path)
    session = screener_login_after_signin(actor, audit_path, runner=session_runner)
    if not session.get("ok"):
        audit.append("screener_scrape_after_signin", {
            "ran": False, "reason": "session not usable: " + str(session.get("reason", ""))[:160],
            "actor": actor})
        return {"ran": False, "session": session}

    if not source_is_stale(source, datetime.now(timezone.utc), every_minutes):
        audit.append("screener_scrape_after_signin", {
            "ran": False, "reason": "saved selection is fresh", "actor": actor})
        return {"ran": False, "session": session}

    try:
        scrape_fn = scrape or make_screener_scraper(source=source)
        result = scrape_fn()
    except Exception as exc:  # noqa: BLE001 - a scrape problem must never break the dashboard
        result = {"ok": False, "reason": f"scrape raised {type(exc).__name__}"}
    audit.append("screener_scrape_after_signin", {
        "ran": True, "ok": bool(result.get("ok")), "count": int(result.get("count") or 0),
        "reason": str(result.get("reason", ""))[:200], "actor": actor})
    return {"ran": True, "session": session, "scrape": result}
