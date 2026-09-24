# my-trading-system — Master Operations README

**Repository:** https://github.com/Ravindhu/my-trading-system

**Architecture (locked):** `Python (pov_engine) → OpenAlgo (server) → Broker → Exchange`

The engine talks to the platform **only** through the public OpenAlgo SDK
(`from openalgo import api`) and its REST/WebSocket endpoints. It never imports
from OpenAlgo's internals and never calls a broker directly.

## Layout

```
my-trading-system/
├── openalgo/                 PRISTINE upstream clone — never hand-edited.
│                             Upgrade with git pull; a re-clone is safe.
├── _restored/                YOUR STATE — the source of truth.
│                               .env      OpenAlgo config (FERNET_SALT, API_KEY_PEPPER, DB urls)
│                               db/       SQLite DBs — broker session tokens live here, encrypted
│                               keys/     Fernet / oauth key material
│                               download/ master-contract symbol cache
├── pov_engine/               YOUR engine. Own package, installed editable.
│                             Talks only via the SDK. No OpenAlgo imports.
├── scripts/
│   ├── start_openalgo.py     launcher; bridges _restored/ into the clone on start
│   ├── run_pov.py            engine launcher
│   ├── upgrade_openalgo.ps1  ONE-COMMAND safe upgrade (market-hours + position gates)
│   ├── smoke_test.py         post-upgrade acceptance gate
│   ├── probe_sdk.py          SDK contract probe
│   └── START_OPENALGO.bat    wrapper for start_openalgo.py
├── patches/                  Custom platform patches (NOT POV — see archive/)
│   ├── openalgo-custom.patch
│   └── openalgo-custom-other.patch
├── archive/                  Retired code. Reference only — never executed.
│   ├── server_pov_nse/       The old server-side POV (ran inside Flask)
│   ├── ubuntu_pov_wiring/    Old deploy scripts that injected POV into app.py
│   ├── scripts_retired/      One-shot migration scripts
│   └── retired_old_openalgo/ The pre-migration tree
└── data/                     engine ledger/state (persistent)
```

## Why `_restored/` is a sibling, not a subfolder

OpenAlgo resolves `.env` and `db/` **relative to its own root**, so state kept in a
subfolder would be invisible to it. Keeping state as a sibling therefore achieves
both goals at once: the clone stays pristine, and the state is immune to a re-clone.
`scripts/start_openalgo.py` is the bridge — it copies `_restored/*` into `openalgo/`
at startup (`bootstrap_restored`).

**CRITICAL — `.env` and `db/` are cryptographically married.** `FERNET_SALT` and
`API_KEY_PEPPER` in `.env` are the KDF inputs that encrypt the broker session tokens
in `db/openalgo.db`. Restore one without the other and OpenAlgo boots fine but
**cannot decrypt your broker login**. They must always travel together.

## Daily operations

| Task | Command |
|---|---|
| Start the platform | `.\scripts\start_openalgo.py` |
| Health check | `openalgo\.venv\Scripts\python.exe scripts\smoke_test.py` |
| SDK contract probe | `openalgo\.venv\Scripts\python.exe scripts\probe_sdk.py` |
| Run the engine | `openalgo\.venv\Scripts\python.exe scripts\run_pov.py` |
| Upgrade platform (after 15:30 IST) | `powershell -ExecutionPolicy Bypass -File scripts\upgrade_openalgo.ps1` |

## Upgrade flow (`scripts\upgrade_openalgo.ps1`)

1. Refuses during NSE market hours (09:15–15:30 IST) or with open positions (unless `-Force`)
2. Backs up `_restored/` state + records the source commit
3. `git -C openalgo pull --ff-only` — aborts on divergence, never force-merges
4. Restores `_restored/` into the clone
5. `pip install -r requirements.txt`
6. **Verifies the `.env`/`db` pairing still decrypts** (catches a partial restore)
7. `smoke_test.py` must PASS — otherwise do not trade

No patch re-application step: nothing POV-related is patched into OpenAlgo.

## API key

Generate in the OpenAlgo UI (Keys & Reports → API Keys). Put it in
`my-trading-system\_restored\.env` as `OPENALGO_API_KEY=...`.

Never hardcode the key in a source file. Scripts resolve it from the environment
first, then from `_restored\.env`. `probe_sdk.py` and `sample_order.py` already
follow this rule.

## Dashboard

The Plotly dashboard runs as its own process (port 8050) and is opened directly.
It is **not** embedded in the OpenAlgo UI — no blueprint, no `app.py` wiring.

## Engine upgrades

`pov_engine` is its own package. After changing engine code:

```powershell
pip install -e .\pov_engine      # if dependencies changed
.\scripts\smoke_test.py           # verify it still imports and passes
```

Nothing in `openalgo/` changes — the engine is a *consumer* of the platform.

## Retired components (do not act on these)

`archive/` holds the old architecture. Kept for reference and forensics only:

- **`server_pov_nse/`** — server-side POV. Ran its loop inside the Flask process via
  `socketio.start_background_task`, so a live execution died on any platform reload.
  Replaced by `pov_engine`, which runs in its own process.
- **`ubuntu_pov_wiring/`** — deploy scripts that copied `patches/pov_terminal.py`
  into `blueprints/` and `sed`-edited `app.py`. Under the current architecture the
  dashboard is opened directly, so no wiring is needed — and those scripts will fail
  their own `die` checks against a pristine clone.
- **`scripts_retired/`** — one-shot migration helpers (`complete_move.ps1`) whose job
  is finished.