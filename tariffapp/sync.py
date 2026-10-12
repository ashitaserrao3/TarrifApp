"""
Load data from the project's data folder - the "backend" way to update the app.

    data/schedule/   the ATC/AOC flight-schedule workbook (the newest .xlsx here is used)
    data/rates/      every Air India / IndiGo rate-list .xlsx you've received

Files already in the database are skipped, and the schedule workbook is only
re-imported when its contents change, so syncing is cheap enough to run
every time the app starts. Because the files live in the repository, the
data survives app restarts (e.g. on Streamlit Community Cloud) and only
people who can change the repository can change the data.
"""

import hashlib
import os

from .etl.loader import load_rate_file
from .importers import import_schedule_workbook

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DATA_DIR = os.path.join(PROJECT_ROOT, "data")


def data_dir():
    return os.environ.get("TARIFFAPP_DATA", DEFAULT_DATA_DIR)


def _xlsx_files(folder):
    if not os.path.isdir(folder):
        return []
    return sorted(os.path.join(folder, f) for f in os.listdir(folder)
                  if f.lower().endswith((".xlsx", ".xlsm")) and not f.startswith(("~$", ".")))


def folder_signature(folder=None):
    """Cheap fingerprint of the data folder (names, sizes, times) - changes when a file is added or replaced."""
    folder = folder or data_dir()
    sig = []
    for sub in ("schedule", "rates"):
        for path in _xlsx_files(os.path.join(folder, sub)):
            st = os.stat(path)
            sig.append((sub, os.path.basename(path), st.st_size, int(st.st_mtime)))
    return tuple(sig)


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def sync_data_folder(conn, folder=None) -> dict:
    """Bring the database up to date with the data folder. Returns a summary."""
    folder = folder or data_dir()
    out = {"schedule": None, "rates_loaded": [], "rates_skipped": 0, "errors": []}

    schedules = _xlsx_files(os.path.join(folder, "schedule"))
    if schedules:
        # Newest by modification time; ties broken by name so the choice is stable.
        path = max(schedules, key=lambda p: (os.path.getmtime(p), p))
        digest = _sha256(path)
        row = conn.execute("SELECT Value FROM AppState WHERE Key = 'schedule_sha256'").fetchone()
        if not row or row[0] != digest:
            try:
                s = import_schedule_workbook(conn, path)
                conn.execute("INSERT INTO AppState (Key, Value) VALUES ('schedule_sha256', ?) "
                             "ON CONFLICT(Key) DO UPDATE SET Value = excluded.Value", (digest,))
                conn.commit()
                out["schedule"] = f"{os.path.basename(path)} ({s['flights']} flights)"
            except Exception as e:  # noqa: BLE001 - reported to the caller
                conn.rollback()
                out["errors"].append(f"{os.path.basename(path)}: {e}")

    for path in _xlsx_files(os.path.join(folder, "rates")):
        r = load_rate_file(conn, path)
        if r["status"] == "ok":
            out["rates_loaded"].append(r["file"])
        elif r["status"] == "skipped":
            out["rates_skipped"] += 1
        else:
            out["errors"].append(f"{r['file']}: {r['message']}")
    return out
