"""SQLite connection helpers."""

import os
import sqlite3

from .schema import build_schema_sql

DEFAULT_AIRLINES = [
    # Code, Name, TariffCarrier, AddOnPerKg, AddOnIncludes
    ("INDIGO", "IndiGo", "IndiGo", 18.0, "X-ray, TSP, handling etc. (approx.)"),
    ("AIRINDIA", "Air India / Air India Express", "Air India", 12.0, "X-ray, TSP, handling etc. (approx.)"),
    ("AKASA", "Akasa Air", None, None, None),
    ("SPICEJET", "SpiceJet", None, None, None),
]


def connect(path: str) -> sqlite3.Connection:
    d = os.path.dirname(os.path.abspath(path))
    os.makedirs(d, exist_ok=True)
    conn = sqlite3.connect(path, detect_types=0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


# Columns added after the first release; older databases get them on start-up.
_ADDED_COLUMNS = {"Flights": ["Source TEXT", "EffectiveFrom TEXT", "EffectiveTo TEXT", "Aircraft TEXT"]}


def init_schema(conn: sqlite3.Connection):
    conn.executescript(build_schema_sql())
    for table, cols in _ADDED_COLUMNS.items():
        have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        for col in cols:
            if col.split()[0] not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col}")
    if conn.execute("SELECT COUNT(*) FROM Airlines").fetchone()[0] == 0:
        conn.executemany(
            "INSERT INTO Airlines (Code, Name, TariffCarrier, AddOnPerKg, AddOnIncludes) VALUES (?,?,?,?,?)",
            DEFAULT_AIRLINES,
        )
    conn.commit()
