"""Remove every non-owner dashboard account. Owner accounts are never touched.

Dry run by default: it only lists the accounts. Add --apply to delete them.

    python scripts/ops/remove_viewer_accounts.py            # list only
    python scripts/ops/remove_viewer_accounts.py --apply    # delete viewers

Plain English: this edits data/dashboard_users.json on this machine. Make a
copy of that file first if you want a backup.
"""
import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from quant.core import dashboard_accounts as acc  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="really delete the viewer accounts")
    parser.add_argument("--users", type=Path, default=acc.DEFAULT_USERS, help="accounts file path")
    args = parser.parse_args()

    names = acc.viewer_account_names(args.users)
    if not names:
        print("No viewer accounts found. Nothing to do.")
        return 0
    print(("Deleting" if args.apply else "Would delete (dry run)") + f" {len(names)} viewer account(s):")
    for name in names:
        print("  - " + name)
    if not args.apply:
        print("Run again with --apply to delete them.")
        return 0
    acc.remove_viewer_accounts(args.users, dry_run=False)
    print("Done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
