# data/ - engine state (never committed)

Created by `scripts/ops/backup_state.py --setup`. The engine writes its runtime files here
(screened candidates, schedule settings, audit trail). Backed up daily by `backup_state.py`.
