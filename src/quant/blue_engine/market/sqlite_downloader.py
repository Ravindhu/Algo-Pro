import json
import os
import time
from datetime import datetime, timedelta

import pandas as pd
from dotenv import load_dotenv
from openalgo import api
from sqlalchemy import MetaData, create_engine

# Load environment variables
load_dotenv()

API_KEY = os.getenv("API_KEY")
DB_NAME = os.getenv("DB_NAME", "amibroker.db")
INTERVAL = os.getenv("INTERVAL", "1m")
HOST = os.getenv("HOST", "http://127.0.0.1:5000")
MAX_RPS = int(os.getenv("MAX_REQUESTS_PER_SECOND", 10))
POLL_INTERVAL = int(os.getenv("POLLING_INTERVAL_SECONDS", 5))
INITIAL_DAYS = int(os.getenv("INITIAL_DAYS", 30))

# Paths
# Script lives in Blue_engine/market/, so the project root is three levels up.
# Runtime database goes under data/openalgo_db/ (gitignored) so it never ends
# up tracked in the repo.
_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
DB_FOLDER = os.path.join(_REPO_ROOT, "data", "openalgo_db")
os.makedirs(DB_FOLDER, exist_ok=True)
SYMBOLS_FILE = os.path.join(os.path.dirname(__file__), "symbols", "sample_nse.csv")

DB_PATH = os.path.join(DB_FOLDER, DB_NAME)
CHECKPOINT_FILE = os.path.join(DB_FOLDER, "checkpoints.json")

# Setup
client = api(api_key=API_KEY, host=HOST)
engine = create_engine(f"sqlite:///{DB_PATH}")
metadata = MetaData()
metadata.reflect(bind=engine)

# Load checkpoints
if os.path.exists(CHECKPOINT_FILE):
    with open(CHECKPOINT_FILE) as f:
        checkpoints = json.load(f)
else:
    checkpoints = {}


def save_checkpoints():
    with open(CHECKPOINT_FILE, "w") as f:
        json.dump(checkpoints, f, indent=2)


def get_symbols():
    if not os.path.exists(SYMBOLS_FILE):
        return []
    df = pd.read_csv(SYMBOLS_FILE, header=None)
    return df[0].dropna().astype(str).str.strip().tolist()


def fetch_and_store(symbol):
    today = datetime.now().strftime("%Y-%m-%d")
    start_date = checkpoints.get(
        symbol, (datetime.now() - pd.Timedelta(days=INITIAL_DAYS)).strftime("%Y-%m-%d")
    )
    end_date = today

    response = client.history(
        symbol=symbol, exchange="NSE", interval=INTERVAL, start_date=start_date, end_date=end_date
    )

    if not isinstance(response, pd.DataFrame) or response.empty:
        print(f"[{symbol}] No data returned or invalid response.")
        return

    df = response
    df.index = pd.to_datetime(df.index)
    df = df[df.index.strftime("%Y-%m-%d") >= start_date]

    if df.empty:
        print(f"[{symbol}] No new rows after filtering.")
        return

    # Convert timestamps from UTC to IST (subtract 5:30 hours)
    ist_timestamps = df.index - timedelta(hours=5, minutes=30)

    df["SYMBOL"] = symbol
    df["DATE"] = ist_timestamps.strftime("%Y-%m-%d %H:%M:%S")
    df = df[["SYMBOL", "DATE", "open", "high", "low", "close", "volume"]]
    df.columns = ["SYMBOL", "DATE", "OPEN", "HIGH", "LOW", "CLOSE", "VOLUME"]

    df.to_sql("stock_data", con=engine, if_exists="append", index=False)

    last_dt = df.index.max().strftime("%Y-%m-%d")
    checkpoints[symbol] = last_dt
    print(f"[{symbol}] Inserted {len(df)} records. Updated checkpoint: {last_dt}")


# ---------------------------------------------------------------------------
# IMPORT SAFETY (audit fix 2026-10-07)
# ---------------------------------------------------------------------------
# This module lives under blue_engine/market/, which core/registry.discover_all()
# walks at import time to auto-register engine classes. The polling loop below
# is a standalone downloader, not library code; leaving it at module level made
# `import blue_engine.core` (and therefore the Dash terminal) spin forever.
# Guard it behind __main__ so discovery is side-effect free.
def _poll_forever() -> None:
    """Main download loop with throttling (never returns)."""
    while True:
        try:
            symbols = get_symbols()
            for i, symbol in enumerate(symbols):
                fetch_and_store(symbol)
                if (i + 1) % MAX_RPS == 0:
                    print("Rate limit reached. Sleeping 1s...")
                    time.sleep(1)
            save_checkpoints()
            time.sleep(POLL_INTERVAL)
        except Exception as e:
            print(f"Error: {e}")
            time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    _poll_forever()
