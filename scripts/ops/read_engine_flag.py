"""Print 1 when auto engine start is ON in the saved settings, otherwise 0.

Read at run time by preopen_launch.sh and SCHEDULED_PREOPEN.cmd, so the
dashboard toggle takes effect without reinstalling the scheduler. Fails closed:
a missing, corrupt or invalid settings file prints 0.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from quant.core.schedule_settings import DEFAULT_PATH, load  # noqa: E402


def engine_flag(path: Path = DEFAULT_PATH) -> str:
    settings, warning = load(path)
    return "1" if (settings.engine_start and warning is None) else "0"


def main(argv: Optional[list] = None) -> int:
    print(engine_flag())
    return 0


if __name__ == "__main__":
    sys.exit(main())
