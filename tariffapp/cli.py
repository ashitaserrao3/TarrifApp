"""Command-line helpers:  flask --app tariffapp <command>"""

import os

import click

from . import db
from .etl.loader import load_rate_file
from .importers import import_legacy_db, import_schedule_workbook


def register(app):
    def conn():
        c = db.connect(app.config["DATABASE"])
        db.init_schema(c)
        return c

    @app.cli.command("load-rates")
    @click.argument("paths", nargs=-1, required=True)
    @click.option("--force", is_flag=True, help="Reload even if the file is already loaded.")
    def load_rates(paths, force):
        """Load Air India / IndiGo rate-list .xlsx files (files or folders)."""
        c = conn()
        files = []
        for p in paths:
            if os.path.isdir(p):
                files += [os.path.join(p, f) for f in sorted(os.listdir(p))
                          if f.lower().endswith(".xlsx") and not f.startswith("~$")]
            else:
                files.append(p)
        for f in files:
            r = load_rate_file(c, f, force=force)
            click.echo(f"  {r['status'].upper():8s} {r['carrier'] or '':10s} {r['file']}  {r['message']}")
            for w in r["warnings"]:
                click.echo(f"           WARN {w}")

    @app.cli.command("import-schedule")
    @click.argument("path")
    def import_schedule(path):
        """Import the flight-schedule workbook (Raw Data, Lists, Add-on Rates, Network Surcharges)."""
        s = import_schedule_workbook(conn(), path)
        click.echo(f"Imported {s['flights']} flights, {s['stations']} stations, "
                   f"{s['airlines']} add-on rows, {s['surcharges']} network surcharges.")

    @app.cli.command("import-legacy")
    @click.argument("path")
    def import_legacy(path):
        """Copy all rate history from an existing CargoRates .sqlite / .accdb database."""
        s = import_legacy_db(conn(), path, log=click.echo)
        click.echo(f"Imported {s['batches']} batches ({s['rows']} rows); skipped {s['skipped']} already present.")
