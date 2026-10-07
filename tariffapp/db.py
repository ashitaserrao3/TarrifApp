"""SQLite connection helpers (usable inside and outside a Flask request)."""

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


def init_schema(conn: sqlite3.Connection):
    conn.executescript(build_schema_sql())
    if conn.execute("SELECT COUNT(*) FROM Airlines").fetchone()[0] == 0:
        conn.executemany(
            "INSERT INTO Airlines (Code, Name, TariffCarrier, AddOnPerKg, AddOnIncludes) VALUES (?,?,?,?,?)",
            DEFAULT_AIRLINES,
        )
    conn.commit()


def get_db() -> sqlite3.Connection:
    """Per-request connection for the Flask app."""
    from flask import current_app, g

    if "db" not in g:
        g.db = connect(current_app.config["DATABASE"])
    return g.db


def close_db(_exc=None):
    from flask import g

    db = g.pop("db", None)
    if db is not None:
        db.close()
