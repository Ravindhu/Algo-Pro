"""Set or reset the local password for the accounting view.

Usage (from the repo root):
    python scripts/set_accounting_password.py                 # set password, default location
    python scripts/set_accounting_password.py --path X.hash   # set password at a custom path
    python scripts/set_accounting_password.py --clear-lockout # remove a lockout only

The password is typed without echo and is never printed or logged. Only a salted scrypt
hash is written, to the git-ignored logs/ folder unless --path or ACCOUNTING_PASSWORD_FILE
says otherwise.
"""
from __future__ import annotations

import argparse
import getpass
import pathlib
import sys

SRC = pathlib.Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from quant.reporting.accounting import (  # noqa: E402  (path set above)
    MIN_PASSWORD_LEN,
    clear_lockout,
    password_file,
    set_password,
)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--path", type=pathlib.Path, default=None, help="password hash file")
    parser.add_argument("--clear-lockout", action="store_true", help="remove lockout and exit")
    args = parser.parse_args(argv)

    if args.clear_lockout:
        clear_lockout(args.path)
        print("lockout cleared")
        return 0

    first = getpass.getpass("New accounting password: ")
    second = getpass.getpass("Repeat password: ")
    if first != second:
        print("passwords do not match; nothing written", file=sys.stderr)
        return 1
    if len(first) < MIN_PASSWORD_LEN:
        print(f"password must be at least {MIN_PASSWORD_LEN} characters; nothing written",
              file=sys.stderr)
        return 1
    set_password(first, args.path)
    clear_lockout(args.path)
    print(f"password set at {password_file(args.path)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
