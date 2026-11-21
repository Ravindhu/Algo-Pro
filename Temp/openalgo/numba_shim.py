# -*- coding: utf-8 -*-
"""Compatibility shim that turns legacy ``@jit``/``@njit`` decorators into no-ops.

The indicator math now lives in a Rust core (``openalgo._oaindicators``) reached via
``openalgo/indicators/_backend.py``; numba/llvmlite are no longer a dependency. This
module remains only so any lingering ``from openalgo.numba_shim import jit`` imports
keep working: the decorators simply return the function unchanged (interpreted), and
no compiled kernels are ever produced.
"""

HAS_NUMBA = False


def _noop_decorator(*args, **kwargs):
    """Return the wrapped function unchanged (supports bare and called forms)."""
    if args and callable(args[0]):
        return args[0]
    return lambda fn: fn


def jit(*args, **kwargs):  # type: ignore[override]
    """No-op stand-in for the legacy numba.jit decorator."""
    return _noop_decorator(*args, **kwargs)


def njit(*args, **kwargs):  # type: ignore[override]
    """No-op stand-in for the legacy numba.njit decorator."""
    return _noop_decorator(*args, **kwargs)


# ``prange`` was used for parallel loops; plain ``range`` is the interpreted fallback.
prange = range
