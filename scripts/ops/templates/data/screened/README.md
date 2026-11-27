# data/screened/ - screener and symbol inputs (never committed)

Created by `scripts/ops/backup_state.py --setup`. These files are the bot's symbol inputs.
You can edit the two JSON files below by hand; the dashboard writes the same files.

- `manual_symbols.json`  owner's manual symbols. `{"symbols": []}` at the start. Added with the
                         Screener stocks panel. Each refresh checks them the same way as screened names
                         (live quote, circuit band, no ASM/GSM flag).
- `source_mode.json`     M and A switches. Both `false` = both paths work (Screener.in and manual).
                         `manual: true` alone = manual only. `automation: true` alone = Screener.in only.
- `last_screen.json`     written by the Screener.in refresh (not a template; created on first refresh).
- `latest.json`          the bot's screen, rebuilt every 5 minutes (not a template).

Backed up daily by `backup_state.py` like the rest of `data/`.
