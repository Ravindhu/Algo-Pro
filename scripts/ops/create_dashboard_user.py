"""Create a dashboard account. The password is never stored in plain text.

    python scripts/ops/create_dashboard_user.py --name Ravindhu
    # password: from DASHBOARD_DEFAULT_PASSWORD, or typed at a prompt (hidden)

Plan: docs/SCHEDULER_PLAN.md (Accounts). Writes data/dashboard_users.json only.
"""
from __future__ import annotations

import argparse
import getpass
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from quant.core.dashboard_accounts import DEFAULT_USERS, create_user  # noqa: E402


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Create a dashboard account.")
    parser.add_argument("--name", required=True)
    parser.add_argument("--users-file", type=Path, default=DEFAULT_USERS)
    args = parser.parse_args(argv)
    password = os.environ.get("DASHBOARD_DEFAULT_PASSWORD") or getpass.getpass("Password: ")
    try:
        create_user(args.name, password, args.users_file)
    except ValueError as exc:
        print(f"REFUSED: {exc}")
        return 2
    print(f"created account {args.name!r} in {args.users_file}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
