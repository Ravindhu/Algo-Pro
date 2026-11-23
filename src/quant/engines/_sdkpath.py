"""
_sdkpath.py - ensures 'import openalgo' resolves to the installed SDK
in site-packages rather than the local platform directory openalgo/.
"""
import sys
import os
import importlib

def ensure_openalgo_sdk_path():
    # Find site-packages directory containing installed openalgo SDK
    site_pkg = None
    for p in list(sys.path):
        if "site-packages" in p:
            candidate = os.path.join(p, "openalgo", "__init__.py")
            if os.path.exists(candidate):
                site_pkg = p
                break

    if site_pkg:
        # Move site-packages to the front of sys.path
        if site_pkg in sys.path:
            sys.path.remove(site_pkg)
        sys.path.insert(0, site_pkg)

        # If local openalgo was already cached without 'api', clear it
        if "openalgo" in sys.modules:
            mod = sys.modules["openalgo"]
            if not hasattr(mod, "api"):
                del sys.modules["openalgo"]

ensure_openalgo_sdk_path()

