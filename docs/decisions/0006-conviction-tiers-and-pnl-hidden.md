# 0006: Conviction tiers sit in Sizing; P&L hidden by default; accounting password

Date: 2026-11-04. Status: accepted (owner approved tiers off by default and a local password).

## Decisions

1. **Tier mapping lives in `src/quant/portfolio/tiers.py` (Sizing lane),** not in `models/`.
   The score is in `models/conviction.py` (Calibrated conviction). The tier map turns that score
   into a size multiplier, which is a sizing step. The earlier plan put tiers under `models/`.
   That was a mistake, corrected here.
2. **Tiers are disabled by default** (`config/conviction_tiers.yaml`, `enabled: false`).
   Values are placeholders until the out-of-sample calibration report (step A4) supports them.
3. **Size = min(base x tier x regime, every hard cap).** Conviction and regime can only reduce
   size. A test checks this over a grid of inputs.
4. **P&L is hidden by default** on the terminal dashboard (`reveal_pnl=False`) and the web
   dashboard (`revealPnl=false`, with a Show button). Position size stays visible.
5. **Accounting view needs a local password** on top of the role check. Only a salted scrypt
   hash is stored, in `logs/` (git-ignored) or the path set by `ACCOUNTING_PASSWORD_FILE`.

## Not decided

- Rate limiting or lockout on repeated wrong passwords. Not built. Needed before real use.
- How the password is set up for real (a setup script). Only the functions exist.
- Definitions for "strategy consistency" and "deviation alerts" on the execution panel.
