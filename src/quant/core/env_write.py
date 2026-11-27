"""Write a value back into the repository .env, atomically, under a lock.

Flow stage: none (shared infrastructure; used by data/screener_session.py)
Status: PHASE 5 IMPLEMENTED (2026-11-26)
Existing code to reuse or wrap (do not copy): src/quant/openalgo_platform/utils/env_check.py
(``update_env_values`` / ``_atomic_replace_text``). That writer lives in the VENDORED
OpenAlgo tree, which is imported only by the OpenAlgo app - the quant package must not
import from it (AGENTS.md: the vendored tree is never a dependency of ours). So the same
four guarantees are reproduced here, in ~60 lines, rather than copied wholesale:

  1. read-modify-write under one process-wide lock, so two writers keep both changes;
  2. every line assigning the key is replaced, duplicates included, because python-dotenv
     applies the LAST assignment - replacing only the first would leave the old value live;
  3. an atomic replace: a temp file in the same folder, then ``os.replace``. A crash
     mid-write leaves the previous .env intact, never a half-written one;
  4. the file's own line endings (CRLF / LF) and every other line are preserved.

SECURITY
--------
* A value is never logged, never returned, and never put in an exception message.
  Error messages name the KEY only.
* A value may not contain a line break or NUL: that would inject a second variable.
* The temp file is created owner-owner readable only where the OS supports it (0600).
* This module writes to ``.env``, which is git-ignored. Nothing is ever committed.

Refusal rules (each one fails closed):
* The key must be a plain upper-case variable name.
* A missing .env file is an error, not a create: two writers racing to create one
  could produce a file that is missing every other secret.
"""
from __future__ import annotations

import os
import re
import tempfile
import threading
from pathlib import Path
from typing import Dict, Mapping

__all__ = ["EnvWriteError", "ENV_WRITE_LOCK", "write_env_values", "set_quoted"]

#: One lock for every .env writer in this process. Reentrant: a caller that holds it
#: across its own read-modify-write can still call ``write_env_values``.
ENV_WRITE_LOCK = threading.RLock()

#: A plain variable name. Mixed case is allowed because the real .env uses
#: ``Screenerdotin_sessionid``; what is refused is a space, ``=`` or a control char.
_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_MAX_VALUE_LEN = 4096


class EnvWriteError(ValueError):
    """A write was refused. The message names the key, never the value."""


def set_quoted(key: str, value: str) -> None:
    """Set ``key`` in ``os.environ`` too, so the running process sees it without a restart."""
    os.environ[str(key)] = str(value)


def _check(key: str, value: str) -> None:
    if not _KEY_RE.match(key):
        raise EnvWriteError(f"refusing to write a malformed environment key: {key!r}")
    if "\n" in value or "\r" in value or "\x00" in value:
        raise EnvWriteError(f"refusing to write a line break into {key}")
    if len(value) > _MAX_VALUE_LEN:
        raise EnvWriteError(f"{key}: value longer than {_MAX_VALUE_LEN} characters")


def _quote(value: str) -> str:
    """Single-quoted unless the value contains a single quote, then double-quoted + escaped."""
    if "'" not in value:
        return "'" + value + "'"
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return '"' + escaped + '"'


def _atomic_write(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` through a temp file + os.replace. Never leaves a partial file."""
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix="." + path.name + "-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as stream:
            stream.write(text)
            stream.flush()
            try:
                os.fsync(stream.fileno())
            except OSError:
                pass  # fsync is a nicety; the replace below is what protects the reader
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass  # Windows: the file inherits the folder ACL
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def write_env_values(path, updates: Mapping[str, str]) -> None:
    """Set each key in ``updates`` in the .env at ``path``. Atomic, locked, value never logged.

    Args:
        path: the .env file. It must already exist.
        updates: key -> new value. A blank value is refused: callers that mean "keep the
            old value" must not pass the key at all.

    Raises:
        EnvWriteError: a bad key, a blank or oversized value, or a missing .env file.
        OSError: the file could not be read or written. The message carries no value.
    """
    target = Path(path)
    if not updates:
        return
    for key, value in updates.items():
        key_text, value_text = str(key), str(value)
        _check(key_text, value_text)
        if not value_text.strip():
            raise EnvWriteError(f"{key_text}: refusing to write an empty value")

    with ENV_WRITE_LOCK:
        if not target.is_file():
            # Do not create it: an empty new .env would silently drop every other secret.
            raise EnvWriteError(f"{target.name} does not exist; refusing to create it from one save")
        with open(target, encoding="utf-8", newline="") as fh:
            content = fh.read()
        eol = "\r\n" if "\r\n" in content else "\n"
        lines = content.splitlines(keepends=True)

        for key, value in updates.items():
            pattern = re.compile(rf"^[ \t]*(?:export[ \t]+)?{re.escape(str(key))}[ \t]*=")
            new_line = f"{str(key)}={_quote(str(value))}"
            replaced = False
            for index, line in enumerate(lines):
                if pattern.match(line):
                    ending = line[len(line.rstrip("\r\n")):]
                    lines[index] = new_line + ending
                    replaced = True
            if not replaced:
                if lines and not lines[-1].endswith(("\n", "\r")):
                    lines[-1] = lines[-1] + eol
                lines.append(new_line + eol)

        _atomic_write(target, "".join(lines))


def write_env_values_or_create(path, updates: Mapping[str, str]) -> None:
    """Like ``write_env_values`` but creates the file (mode 0600) when it does not exist yet.

    Used for the repository-root ``.env``, which the readers load (2026-11-28, item 2: both
    copies of the credentials are kept in step). Same validation and atomic write as above.
    """
    target = Path(path)
    if target.is_file():
        write_env_values(target, updates)
        return
    lines = []
    for key, value in updates.items():
        _check(key, value)
        if not value.strip():
            raise EnvWriteError(f"refusing to write a blank value for {key}")
        lines.append(f"{key}={_quote(value)}\n")
    _atomic_write(target, "".join(lines))
