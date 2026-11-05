# src/quant/models

Signal and conviction. Advice and direction only. Models never place orders.

- `conviction.py`: six-condition conviction score. DONE (tests pass).
- `calibration.py`: Platt fitter. PARTIAL. Calibration report needs real data (X-11).
- `factors.py`: momentum and profit factor wrapped. PARTIAL.
- `regime.py`: regime detector. Kept local (decision 0005). PENDING.
- `signal.py`, `meta_label.py`: direction, stop, time limit, take-or-skip. PENDING.
- `events.py`: event scoring, advice only, with a deterministic fallback (X-17). PENDING.

Rule: no model goes live without a registry record and human sign-off (X-19).
