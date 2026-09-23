# my-trading-system - Master Operations README

**POV engine is upgrade-proof.** OpenAlgo is a *consumed service*; everything of yours
lives beside it and re-wires itself after upgrades.

## Architecture
```
my-trading-system/
├── docker-compose.yml        future containerized launch (Docker not yet installed)
├── .env                      OPENALGO_API_KEY (paste the key generated in the OpenAlgo UI)
├── openalgo/                 upstream platform - NEVER commit to it; upgrade via git pull
├── pov_engine/               YOUR engine (own git repo, installable: pip install -e ./pov_engine)
├── patches/
│   ├── pov_terminal.py       canonical blueprint (source of truth)
│   └── apply_patches.ps1     re-wires app.py + blueprint after every upgrade (idempotent)
├── scripts/
│   ├── run_pov.py            engine launcher (canonical copy)
│   ├── upgrade_openalgo.ps1  ONE-COMMAND SAFE UPGRADE (market-hours + position gates)
│   └── smoke_test.py         post-upgrade acceptance gate
└── data/                     engine ledger/state (persistent)
```
NOTE: openalgo is now physically located at my-trading-system\openalgo. A junction at
C:\Users\KanagalakshmiM\openalgo points to it, so the venv and any old absolute paths
keep working. Plan Step 1 is now 100% complete.

## Daily operations
| Task | Command |
|---|---|
| Check wiring | powershell -ExecutionPolicy Bypass -File patches\apply_patches.ps1 -CheckOnly |
| Health check | openalgo\.venv\Scripts\python.exe scripts\smoke_test.py |
| Upgrade platform (after 15:30 IST) | powershell -ExecutionPolicy Bypass -File scripts\upgrade_openalgo.ps1 |
| Run engine (one-off order) | openalgo\.venv\Scripts\python.exe scripts\run_pov.py |

## Upgrade flow (what scripts\upgrade_openalgo.ps1 does)
1. Refuses during NSE market hours (09:15-15:30 IST) or with open positions (unless -Force)
2. Backs up .env + db/ to _backup\upgrade_<stamp>\
3. git -C openalgo pull --ff-only (aborts on divergence - never force-merges)
4. pip install -r requirements.txt (openalgo venv)
5. patches\apply_patches.ps1 (re-wires the blueprint into app.py)
6. smoke_test.py must PASS (exit 0) - otherwise do not trade

## API key
The platform API key is generated in the OpenAlgo UI (Keys & Reports / API Keys).
Paste it into my-trading-system\.env as OPENALGO_API_KEY=... for the SDK/smoke tests.

## Engine upgrades
pov_engine has its own git repo. After changing engine code:
  git -C pov_engine commit -am "change"
Nothing in openalgo/ changes - the wiring is data, not code.