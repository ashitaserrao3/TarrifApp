"""Command-line helpers:  flask --app tariffapp <command>"""

import csv
import datetime as dt
import os

import click

from . import db, recommend
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
        _load(c, paths, force)

    @app.cli.command("recommend")
    @click.option("--weight", type=click.FloatRange(min=0, min_open=True), default=recommend.DEFAULT_WEIGHT, show_default=True,
                  help="Shipment chargeable weight in kg.")
    @click.option("--date", "as_of", type=click.DateTime(["%Y-%m-%d"]), help="Rates as of (default: today).")
    @click.option("--origin", "origins", multiple=True, help="Only these origins (repeatable).")
    @click.option("--load", "load_paths", multiple=True,
                  help="Load these rate files / folders first (already-loaded files are skipped).")
    @click.option("--out", type=click.Path(dir_okay=False), help="Write the full report to this CSV file.")
    def recommend_cmd(weight, as_of, origins, load_paths, out):
        """Pick the cheapest airline on every route and show the saving."""
        c = conn()
        if load_paths:
            _load(c, load_paths, False)
        as_of = as_of.date() if as_of else dt.date.today()
        _, picks = recommend.recommend(c, as_of, weight, origins={o.upper() for o in origins} or None)
        s = recommend.summary(picks)
        click.echo(f"Airline picks for a {weight:g} kg shipment, rates as of {as_of:%d-%b-%Y}")
        click.echo(f"{s['lanes']} routes, {s['contested']} with 2+ airlines priced, "
                   f"total saving Rs {s['total_saving']:,.0f} (one shipment per route)")
        for a, n in s["wins"]:
            click.echo(f"  {a:20s} cheapest on {n} route(s), saves Rs {s['saving_by_airline'][a]:,.0f}")
        click.echo("")
        for p in picks:
            w = p.winner
            line = f"  {p.origin}-{p.dest:5s} {p.verdict:20s}"
            if w:
                line += f" {w.airline:14s} {w.best.flight['FlightNo']:8s} Rs {w.per_kg:7.2f}/kg"
            if p.saving is not None:
                line += f"  saves Rs {p.saving:,.0f} ({p.saving_pct:.1f}%) vs {p.runner_up.airline}"
            click.echo(line)
        if out:
            with open(out, "w", newline="", encoding="utf-8-sig") as f:
                wr = csv.writer(f)
                wr.writerow(recommend.EXPORT_HEADER)
                for p in picks:
                    wr.writerow(recommend.export_row(p))
            click.echo(f"\nReport written to {out}")

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


def _load(c, paths, force):
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
