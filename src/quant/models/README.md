# src/quant/models

Signal and conviction. Advice and direction only. Models never place orders.

- `conviction.py`: six-condition conviction score. DONE (tests pass).
- `calibration.py`: Platt fitter. PARTIAL. Calibration report needs real data (X-11).
- `factors.py`: momentum and profit factor wrapped. PARTIAL.
- `regime.py`: regime detector. Kept local (decision 0005). PENDING.
- `signal.py`: direction, stop, time limit. PENDING.
- `meta_label.py`: moved to `Temp/Stag/meta_label/` on 2026-11-26 (staged, not wired). Needs owner decision and evidence before it returns.
- `events/scoring.py`: event scoring, advice only. Keyword fallback DONE (X-17). Optional LLM scorer is not wired to a caller yet.

Rule: no model goes live without a registry record and human sign-off (X-19).
