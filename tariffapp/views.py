import csv
import datetime as dt
import io
import os
from collections import Counter

from jinja2 import Undefined

from flask import (Blueprint, Response, abort, current_app, flash, jsonify, redirect,
                   render_template, request, url_for)
from werkzeug.utils import secure_filename

from .db import get_db
from .etl.loader import delete_batch, load_rate_file
from .importers import DAYS, import_schedule_workbook
from . import queries
from .linker import GENERAL_PRODUCTS, STATUS_CHECK, STATUS_NO_RATE, STATUS_NO_TARIFF, STATUS_OK
from .schema import CARRIER_TABLES

bp = Blueprint("main", __name__)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _blank(v):
    return v is None or v == "" or isinstance(v, Undefined)


def register_filters(app):
    @app.template_filter("inr")
    def inr(v, decimals=0):
        if _blank(v):
            return ""
        try:
            v = float(v)
        except (TypeError, ValueError):
            return v
        return f"{v:,.{decimals}f}"

    @app.template_filter("num")
    def num(v):
        if _blank(v):
            return ""
        try:
            f = float(v)
        except (TypeError, ValueError):
            return v
        return f"{f:g}" if f == int(f) else f"{f:.2f}"

    @app.template_filter("dmy")
    def dmy(v):
        if _blank(v) or not v:
            return ""
        if isinstance(v, str):
            try:
                v = dt.date.fromisoformat(v[:10])
            except ValueError:
                return v
        return v.strftime("%d-%b-%y")

    @app.context_processor
    def inject():
        return {"DAYS": DAYS, "STATUS_OK": STATUS_OK, "STATUS_CHECK": STATUS_CHECK,
                "STATUS_NO_RATE": STATUS_NO_RATE, "STATUS_NO_TARIFF": STATUS_NO_TARIFF}


def _as_of():
    v = request.args.get("date")
    if v:
        try:
            return dt.date.fromisoformat(v)
        except ValueError:
            flash(f"Ignoring invalid date '{v}'.", "warn")
    return dt.date.today()




def _float(v):
    try:
        return float(v) if v not in (None, "") else None
    except ValueError:
        return None




# ---------------------------------------------------------------------------
# Dashboard - route lookup
# ---------------------------------------------------------------------------

@bp.route("/")
def dashboard():
    conn = get_db()
    hubs, stations = queries.stations(conn)
    if not stations:
        return render_template("empty.html")
    names = {s["Code"]: s["Name"] for s in stations}
    origin = (request.args.get("origin") or (hubs[0]["Code"] if hubs else stations[0]["Code"])).upper()
    dest = (request.args.get("dest") or "").upper()
    if not dest:
        dest = next((s["Code"] for s in stations if s["Code"] == "DEL" and s["Code"] != origin),
                    next((s["Code"] for s in stations if s["Code"] != origin), origin))
    as_of = _as_of()
    weight = _float(request.args.get("weight"))

    eng, results, status, weekly = queries.price_route(conn, origin, dest, as_of)

    rows = []
    for r in results:
        c45, u45 = r.cpkg(45)
        c100, u100 = r.cpkg(100)
        rows.append({"r": r, "cpkg45": c45, "used45": u45, "cpkg100": c100, "used100": u100,
                     "total": r.total_cost(weight) if weight else None})

    def cheapest(key):
        vals = [x[key] for x in rows if x[key] is not None and x["r"].status == STATUS_OK]
        return min(vals) if vals else None

    best = {"cpkg45": cheapest("cpkg45"), "cpkg100": cheapest("cpkg100"), "total": cheapest("total")}
    batches = {c: eng.batch(c) for c in CARRIER_TABLES}
    return render_template("dashboard.html", hubs=hubs, stations=stations, names=names, origin=origin,
                           dest=dest, as_of=as_of, weight=weight, rows=rows, weekly=weekly,
                           status=status, best=best, batches=batches)


# ---------------------------------------------------------------------------
# All flights with their linked rate (the old 'Flight Rates' sheet)
# ---------------------------------------------------------------------------



@bp.route("/flights")
def flights():
    conn = get_db()
    as_of = _as_of()
    results = queries.all_priced(conn, as_of)
    counts = Counter(r.status for r in results)
    f = {k: (request.args.get(k) or "").upper() for k in ("origin", "dest", "airline", "status")}
    q = (request.args.get("q") or "").strip().upper()
    shown = [r for r in results
             if (not f["origin"] or r.flight["Origin"] == f["origin"])
             and (not f["dest"] or r.flight["Destination"] == f["dest"])
             and (not f["airline"] or r.flight["Airline"] == f["airline"])
             and (not f["status"] or r.status == f["status"])
             and (not q or q in r.flight["FlightNo"].upper())]
    hubs, stations = queries.stations(conn)
    airlines = [r[0] for r in conn.execute("SELECT DISTINCT Airline FROM Flights ORDER BY 1")]
    return render_template("flights.html", results=shown, total=len(results), counts=counts, f=f, q=q,
                           as_of=as_of, hubs=hubs, stations=stations, airlines=airlines)


@bp.route("/flights.csv")
def flights_csv():
    conn = get_db()
    as_of = _as_of()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(queries.FLIGHT_EXPORT_HEADER)
    for r in queries.all_priced(conn, as_of):
        w.writerow(queries.flight_export_row(r))
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment; filename=flight_rates_{as_of}.csv"})


# ---------------------------------------------------------------------------
# Exceptions - flights that need a human look
# ---------------------------------------------------------------------------

@bp.route("/exceptions")
def exceptions():
    conn = get_db()
    as_of = _as_of()
    return render_template("exceptions.html", as_of=as_of, **queries.exceptions(conn, as_of))


# ---------------------------------------------------------------------------
# Rate history for one lane
# ---------------------------------------------------------------------------

@bp.route("/history")
def history():
    conn = get_db()
    carrier = request.args.get("carrier") or "IndiGo"
    if carrier not in CARRIER_TABLES:
        abort(404)
    products = [p for p in CARRIER_TABLES[carrier] if p != "Other"]
    product = request.args.get("product") or GENERAL_PRODUCTS[carrier][0]
    if product not in CARRIER_TABLES[carrier]:
        product = products[0]
    origin = (request.args.get("origin") or "").upper()
    dest = (request.args.get("dest") or "").upper()
    hubs, stations = queries.stations(conn)
    rows, series = [], []
    if origin and dest:
        rows = queries.lane_history(conn, carrier, product, origin, dest)
        # one line per distinct condition (route rate, time windows, flight lists...)
        by_cond = {}
        for r in rows:
            by_cond.setdefault(queries.history_label(r), []).append(r)
        for label, rs in by_cond.items():
            series.append({"label": label, "points": [
                {"x": (r["EffectiveDate"] or "")[:10], "N": r["RateN"], "Q45": r["RateQ45"],
                 "Q100": r["RateQ100"], "Q300": r["RateQ300"]} for r in rs]})
    return render_template("history.html", carrier=carrier, product=product, products=products, origin=origin,
                           dest=dest, rows=rows, series=series, hubs=hubs, stations=stations,
                           carriers=list(CARRIER_TABLES))


@bp.route("/api/lanes")
def api_lanes():
    """Destinations that have rates from an origin (for the history picker)."""
    conn = get_db()
    carrier = request.args.get("carrier") or "IndiGo"
    product = request.args.get("product") or "GCR"
    origin = (request.args.get("origin") or "").upper()
    if carrier not in CARRIER_TABLES or product not in CARRIER_TABLES[carrier]:
        abort(400)
    t = f"{queries.CARRIER_PREFIX[carrier]}_{product}"
    return jsonify([r[0] for r in conn.execute(
        f"SELECT DISTINCT DestCode FROM {t} WHERE OriginCode = ? ORDER BY 1", (origin,))])


# ---------------------------------------------------------------------------
# Tariff browser - look inside any loaded rate file
# ---------------------------------------------------------------------------

@bp.route("/tariffs")
def tariffs():
    conn = get_db()
    batches = [dict(r) for r in conn.execute(
        "SELECT * FROM LoadBatches WHERE Status='OK' ORDER BY EffectiveDate DESC, BatchID DESC")]
    if not batches:
        return render_template("tariffs.html", batches=[], batch=None)
    bid = request.args.get("batch", type=int) or batches[0]["BatchID"]
    batch = next((b for b in batches if b["BatchID"] == bid), batches[0])
    carrier = batch["Carrier"]
    if carrier not in CARRIER_TABLES:
        abort(404)
    counts = queries.batch_product_counts(conn, batch)
    product = request.args.get("product")
    if product not in counts:
        product = next((p for p in CARRIER_TABLES[carrier] if counts[p]), CARRIER_TABLES[carrier][0])
    origin = (request.args.get("origin") or "").upper()
    dest = (request.args.get("dest") or "").upper()
    limit = 500
    rows = queries.tariff_rows(conn, batch, product, origin, dest, limit=limit + 1)
    truncated = len(rows) > limit
    rows = rows[:limit]
    cond_col, extra = queries.CONDITION_COL[carrier], queries.EXTRA_COLS[carrier]
    return render_template("tariffs.html", batches=batches, batch=batch, counts=counts, product=product,
                           rows=rows, truncated=truncated, limit=limit, origin=origin, dest=dest,
                           cond_col=cond_col, extra=extra, carrier=carrier)


@bp.route("/notes")
def notes():
    conn = get_db()
    return render_template("notes.html", **queries.notes(conn))


# ---------------------------------------------------------------------------
# Uploads + load history
# ---------------------------------------------------------------------------

@bp.route("/uploads", methods=["GET", "POST"])
def uploads():
    conn = get_db()
    if request.method == "POST":
        kind = request.form.get("kind")
        files = [f for f in request.files.getlist("files") if f and f.filename]
        if not files:
            flash("Choose at least one .xlsx file.", "warn")
            return redirect(url_for("main.uploads"))
        for f in files:
            name = os.path.basename(f.filename)
            if not name.lower().endswith((".xlsx", ".xlsm")):
                flash(f"{name}: not an .xlsx file - skipped.", "error")
                continue
            folder = current_app.config["UPLOAD_FOLDER"]
            stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
            path = os.path.join(folder, f"{stamp}_{secure_filename(name) or 'upload.xlsx'}")
            f.save(path)
            try:
                if kind == "schedule":
                    s = import_schedule_workbook(conn, path, filename=name)
                    flash(f"{name}: imported {s['flights']} flights, {s['stations']} stations, "
                          f"{s['airlines']} add-on rates, {s['surcharges']} network surcharges.", "ok")
                else:
                    eff = request.form.get("effective_date")
                    eff = dt.date.fromisoformat(eff) if eff else None
                    r = load_rate_file(conn, path, filename=name, force=bool(request.form.get("force")),
                                       effective_date=eff)
                    level = {"ok": "ok", "skipped": "warn"}.get(r["status"], "error")
                    eff_s = r["effective_date"].strftime("%d-%b-%y") if r["effective_date"] else "unknown date"
                    flash(f"{name}: {r['carrier'] or ''} {r['message']} (effective {eff_s})", level)
                    for w in r["warnings"]:
                        flash(f"{name}: {w}", "warn")
            except Exception as e:  # noqa: BLE001 - show the problem to the user
                conn.rollback()
                flash(f"{name}: {e}", "error")
        return redirect(url_for("main.uploads"))

    batches = [dict(r) for r in conn.execute(
        "SELECT * FROM LoadBatches ORDER BY BatchID DESC")]
    imports = [dict(r) for r in conn.execute("SELECT * FROM ScheduleImports ORDER BY ImportID DESC LIMIT 10")]
    return render_template("uploads.html", batches=batches, imports=imports)


@bp.route("/batches/<int:batch_id>/delete", methods=["POST"])
def batch_delete(batch_id):
    conn = get_db()
    b = conn.execute("SELECT * FROM LoadBatches WHERE BatchID=?", (batch_id,)).fetchone()
    if not b:
        abort(404)
    delete_batch(conn, batch_id)
    flash(f"Deleted {b['SourceFileName']} and all its rows.", "ok")
    return redirect(url_for("main.uploads"))


# ---------------------------------------------------------------------------
# Settings: surcharges, airlines/add-ons, stations
# ---------------------------------------------------------------------------

@bp.route("/settings")
def settings():
    conn = get_db()
    surcharges = [dict(r) for r in conn.execute(
        "SELECT * FROM NetworkSurcharges ORDER BY Carrier, AppliesTo, Station, SurchargeID")]
    airlines = [dict(r) for r in conn.execute("SELECT * FROM Airlines ORDER BY Code")]
    stations = [dict(r) for r in conn.execute("SELECT * FROM Stations ORDER BY IsHub DESC, COALESCE(SortOrder,999), Code")]
    today = dt.date.today().isoformat()
    return render_template("settings.html", surcharges=surcharges, airlines=airlines, stations=stations,
                           carriers=list(CARRIER_TABLES), today=today)


@bp.route("/settings/surcharges", methods=["POST"])
def surcharge_save():
    conn = get_db()
    fm = request.form
    sid = fm.get("SurchargeID", type=int)
    if fm.get("action") == "delete" and sid:
        conn.execute("DELETE FROM NetworkSurcharges WHERE SurchargeID=?", (sid,))
        conn.commit()
        flash("Surcharge deleted.", "ok")
        return redirect(url_for("main.settings") + "#surcharges")
    rate = _float(fm.get("RatePerKg"))
    applies = fm.get("AppliesTo") or "All"
    station = (fm.get("Station") or "").strip().upper() or None
    if fm.get("Carrier") not in CARRIER_TABLES or rate is None:
        flash("Carrier and a numeric Rs/kg are required.", "error")
        return redirect(url_for("main.settings") + "#surcharges")
    if applies != "All" and not station:
        flash(f"'{applies}' surcharges need a station (or DEL-COK style route).", "error")
        return redirect(url_for("main.settings") + "#surcharges")
    vals = (fm.get("Carrier"), applies, station if applies != "All" else None, rate,
            fm.get("ValidFrom") or None, fm.get("ValidTo") or None, fm.get("Reason") or None,
            1 if fm.get("Active") else 0, fm.get("Label") or None)
    if sid:
        conn.execute("UPDATE NetworkSurcharges SET Carrier=?, AppliesTo=?, Station=?, RatePerKg=?, ValidFrom=?, "
                     "ValidTo=?, Reason=?, Active=?, Label=? WHERE SurchargeID=?", (*vals, sid))
    else:
        conn.execute("INSERT INTO NetworkSurcharges (Carrier, AppliesTo, Station, RatePerKg, ValidFrom, ValidTo, "
                     "Reason, Active, Label) VALUES (?,?,?,?,?,?,?,?,?)", vals)
    conn.commit()
    flash("Surcharge saved.", "ok")
    return redirect(url_for("main.settings") + "#surcharges")


@bp.route("/settings/airlines", methods=["POST"])
def airline_save():
    conn = get_db()
    fm = request.form
    code = (fm.get("Code") or "").strip().upper()
    if not code:
        flash("Airline code is required.", "error")
        return redirect(url_for("main.settings") + "#airlines")
    if fm.get("action") == "delete":
        conn.execute("DELETE FROM Airlines WHERE Code=?", (code,))
    else:
        tc = fm.get("TariffCarrier") or None
        if tc and tc not in CARRIER_TABLES:
            tc = None
        conn.execute(
            "INSERT INTO Airlines (Code, Name, TariffCarrier, AddOnPerKg, AddOnIncludes) VALUES (?,?,?,?,?) "
            "ON CONFLICT(Code) DO UPDATE SET Name=excluded.Name, TariffCarrier=excluded.TariffCarrier, "
            "AddOnPerKg=excluded.AddOnPerKg, AddOnIncludes=excluded.AddOnIncludes",
            (code, fm.get("Name") or code, tc, _float(fm.get("AddOnPerKg")), fm.get("AddOnIncludes") or None))
    conn.commit()
    flash(f"Airline {code} saved.", "ok")
    return redirect(url_for("main.settings") + "#airlines")


@bp.route("/settings/stations", methods=["POST"])
def station_save():
    conn = get_db()
    fm = request.form
    code = (fm.get("Code") or "").strip().upper()
    if not code:
        flash("Station code is required.", "error")
        return redirect(url_for("main.settings") + "#stations")
    if fm.get("action") == "delete":
        conn.execute("DELETE FROM Stations WHERE Code=?", (code,))
    else:
        conn.execute(
            "INSERT INTO Stations (Code, Name, IsHub, TariffCode) VALUES (?,?,?,?) "
            "ON CONFLICT(Code) DO UPDATE SET Name=excluded.Name, IsHub=excluded.IsHub, "
            "TariffCode=excluded.TariffCode",
            (code, fm.get("Name") or None, 1 if fm.get("IsHub") else 0,
             (fm.get("TariffCode") or "").strip().upper() or None))
    conn.commit()
    flash(f"Station {code} saved.", "ok")
    return redirect(url_for("main.settings") + "#stations")
