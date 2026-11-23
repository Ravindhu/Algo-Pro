# Database engine and optional historical-data reference

`DatabaseEngine` owns the system's data-ingestion and alignment interfaces. It
continues to persist OHLCV through the existing shared `PriceCache`; it does not
import or depend on the upstream repository below.

## Pinned upstream reference

The optional pristine checkout is at
`Blue_engine/database/vendor/indian-market-historical-data/`. It is shallow and
sparse: only the upstream repository metadata and `scripts/` are materialized.
The large historical `data/` directory is intentionally not downloaded until a
specific research need and data-use review justify it.

The upstream LICENSE permits use only for personal, non-commercial, and
educational research. It expressly prohibits commercial use, including
commercial automated trading systems, without prior written permission. Do not
use this repository's code or data for commercial trading, live execution, or
redistribution unless the rights holder grants written permission and the use
has been reviewed. No engine integration is enabled by this vendoring step.

The checkout is ignored by Git. Its expected upstream commit is tracked in
`vendor/INDIAN_MARKET_DATA_UPSTREAM_PIN`; local upstream files must remain
unmodified.

### Clone or restore the sparse checkout

From the repository root:

```powershell
git clone --filter=blob:none --depth 1 --sparse --branch main `
  https://github.com/aswinv90/indian-market-historical-data.git `
  Blue_engine\database\vendor\indian-market-historical-data
git -C Blue_engine\database\vendor\indian-market-historical-data sparse-checkout set --cone scripts
python Blue_engine\database\vendor_pin.py --check
```

If the current upstream branch has advanced beyond the tracked pin, stop and
review the changes before running `--update-pin`. The update helper refuses
dirty checkouts and only uses `git pull --ff-only`; it never merges divergent
history:

```powershell
python Blue_engine\database\vendor_pin.py --diff
python Blue_engine\database\vendor_pin.py --update-pin
```

`--update-pin` changes the tracked pin after a successful fast-forward. Review
the upstream diff and keep any project-specific adapters in this package, not
in the upstream checkout.
