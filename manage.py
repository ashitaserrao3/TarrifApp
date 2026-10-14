#!/usr/bin/env python3
"""
Bulk data commands (everything here can also be done from the dashboard's
sidebar, one upload at a time):

    python manage.py sync                                  # load everything new in the data/ folder
    python manage.py load-rates  <file-or-folder> [...]   # Air India / IndiGo rate lists
    python manage.py import-schedule <workbook.xlsx>       # flights, add-ons, surcharges
    python manage.py import-legacy <CargoRates.accdb|.sqlite>   # all old rate history at once
"""

import argparse
import os

from tariffapp import database_path, db, sync
from tariffapp.etl.loader import load_rate_file
from tariffapp.importers import import_legacy_db, import_schedule_workbook


def main():
    ap = argparse.ArgumentParser(description="Cargo Tariffs data commands.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("sync", help="Load everything new in the data/ folder (what the app does on start).")
    p = sub.add_parser("load-rates", help="Load rate-list .xlsx files (files or folders).")
    p.add_argument("paths", nargs="+")
    p.add_argument("--force", action="store_true", help="Reload even if already loaded.")
    sub.add_parser("import-schedule", help="Import the flight-schedule workbook.").add_argument("path")
    sub.add_parser("import-legacy", help="Copy rate history from an old CargoRates database.").add_argument("path")
    args = ap.parse_args()

    conn = db.connect(database_path())
    db.init_schema(conn)
    print(f"Database: {database_path()}")

    if args.cmd == "sync":
        r = sync.sync_data_folder(conn)
        print(f"Schedule: {r['schedule'] or 'unchanged'}")
        if r.get("dgca"):
            print(f"Flights: {r['dgca']}")
        for h in r["history"]:
            print(f"History: {h}")
        print(f"Rate files loaded: {len(r['rates_loaded'])}, already loaded: {r['rates_skipped']}")
        for e in r["errors"]:
            print(f"  ERROR {e}")
    elif args.cmd == "load-rates":
        files = []
        for p in args.paths:
            if os.path.isdir(p):
                files += [os.path.join(p, f) for f in sorted(os.listdir(p))
                          if f.lower().endswith(".xlsx") and not f.startswith("~$")]
            else:
                files.append(p)
        for f in files:
            r = load_rate_file(conn, f, force=args.force)
            print(f"  {r['status'].upper():8s} {r['carrier'] or '':10s} {r['file']}  {r['message']}")
            for w in r["warnings"]:
                print(f"           WARN {w}")
    elif args.cmd == "import-schedule":
        s = import_schedule_workbook(conn, args.path)
        print(f"Imported {s['flights']} flights, {s['stations']} stations, "
              f"{s['airlines']} add-on rows, {s['surcharges']} network surcharges.")
    else:
        s = import_legacy_db(conn, args.path, log=print)
        print(f"Imported {s['batches']} batches ({s['rows']} rows); skipped {s['skipped']} already present.")


if __name__ == "__main__":
    main()
