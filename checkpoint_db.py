from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = Path(os.getenv("DB_PATH", "broker_tracker.db"))

if not DB_PATH.is_absolute():
    DB_PATH = BASE_DIR / DB_PATH

if DB_PATH.exists():
    con = sqlite3.connect(DB_PATH)
    try:
        row = con.execute("PRAGMA wal_checkpoint(TRUNCATE);").fetchone()
        print("SQLite WAL checkpoint:", row)
    finally:
        con.close()
else:
    print("DB belum ada:", DB_PATH)
