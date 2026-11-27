# _restored/ - your local state (never committed)

This folder is created by `scripts/ops/backup_state.py --setup`. It holds:

- `.env`      OpenAlgo config and secrets. Put your ORIGINAL file here. Never generate a new one.
- `db/`       SQLite databases copied to the OpenAlgo tree on start.
- `keys/`     key material.
- `download/` master-contract cache (optional).

`.env` and `db/` are a matched pair. Restore both from the same backup.
