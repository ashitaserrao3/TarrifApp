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
from .linker import (GENERAL_PRODUCTS, STATUS_CHECK, STATUS_NO_RATE, STATUS_NO_TARIFF, STATUS_OK,
                     TariffEngine, flights_for_route)
from .schema import CARRIER_PREFIX, CARRIER_TABLES

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


def _stations(conn):
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM Stations ORDER BY COALESCE(SortOrder, 999), Code")]
    hubs = [r for r in rows if r["IsHub"]]
    if not hubs:  # nothing marked yet: every origin in the schedule is a hub
        origins = {r[0] for r in conn.execute("SELECT DISTINCT Origin FROM Flights")}
        hubs = [r for r in rows if r["Code"] in origins]
    return hubs, rows


def _float(v):
    try:
        return float(v) if v not in (None, "") else None
    except ValueError:
        return None


def _batch_summary(conn):
    out = {}
    for carrier in CARRIER_TABLES:
        r = conn.execute(
            "SELECT * FROM LoadBatches WHERE Carrier=? AND Status='OK' ORDER BY EffectiveDate DESC, BatchID DESC "
            "LIMIT 1", (carrier,)).fetchone()
        out[carrier] = dict(r) if r else None
    return out


# ---------------------------------------------------------------------------
# Dashboard - route lookup
# ---------------------------------------------------------------------------

@bp.route("/")
def dashboard():
    conn = get_db()
    hubs, stations = _stations(conn)
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

    eng = TariffEngine(conn, as_of)
    results = eng.price_all(flights_for_route(conn, origin, dest))
    weekly = sum(sum(int(r.flight[d] or 0) for d in DAYS) for r in results)
    status = "SAME CITY" if origin == dest else ("NO SERVICE" if not results else "ACTIVE")

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

def _all_priced(conn, as_of):
    flights = [dict(r) for r in conn.execute(
        "SELECT * FROM Flights ORDER BY Origin, Destination, DepTime, FlightNo")]
    return TariffEngine(conn, as_of).price_all(flights)


@bp.route("/flights")
def flights():
    conn = get_db()
    as_of = _as_of()
    results = _all_priced(conn, as_of)
    counts = Counter(r.status for r in results)
    f = {k: (request.args.get(k) or "").upper() for k in ("origin", "dest", "airline", "status")}
    q = (request.args.get("q") or "").strip().upper()
    shown = [r for r in results
             if (not f["origin"] or r.flight["Origin"] == f["origin"])
             and (not f["dest"] or r.flight["Destination"] == f["dest"])
             and (not f["airline"] or r.flight["Airline"] == f["airline"])
             and (not f["status"] or r.status == f["status"])
             and (not q or q in r.flight["FlightNo"].upper())]
    hubs, stations = _stations(conn)
    airlines = [r[0] for r in conn.execute("SELECT DISTINCT Airline FROM Flights ORDER BY 1")]
    return render_template("flights.html", results=shown, total=len(results), counts=counts, f=f, q=q,
                           as_of=as_of, hubs=hubs, stations=stations, airlines=airlines)


@bp.route("/flights.csv")
def flights_csv():
    conn = get_db()
    as_of = _as_of()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Origin", "Destination", "Airline", "Flight No", "Dep Time", *DAYS, "Weekly Freq", "Product",
                "Effective Date", "Rate File", "Min", "N", "45+", "100+", "300+", "500+", "1000+", "Status",
                "Tariff Condition", "Match Note", "Surcharge Rs/kg", "Surcharge Summary", "Add-on Rs/kg",
                "CPKG 45+", "CPKG 100+"])
    for r in _all_priced(conn, as_of):
        fl, rt = r.flight, r.rates
        w.writerow([fl["Origin"], fl["Destination"], fl["Airline"], fl["FlightNo"], fl["DepTime"],
                    *[fl[d] for d in DAYS], sum(int(fl[d] or 0) for d in DAYS), r.product or "",
                    r.effective_date or "", r.source_file or "", rt["Min"], rt["N"], rt["45"], rt["100"],
                    rt["300"], rt["500"], rt["1000"], r.status,
                    r.condition.text if r.condition and r.condition.text else "", r.match_note,
                    r.surcharge_total if r.rate_row else "", r.surcharge_summary, r.addon or "",
                    r.cpkg(45)[0] or "", r.cpkg(100)[0] or ""])
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment; filename=flight_rates_{as_of}.csv"})


# ---------------------------------------------------------------------------
# Exceptions - flights that need a human look
# ---------------------------------------------------------------------------

@bp.route("/exceptions")
def exceptions():
    conn = get_db()
    as_of = _as_of()
    results = _all_priced(conn, as_of)
    check = [r for r in results if r.status in (STATUS_CHECK, STATUS_NO_RATE)]
    ended = [r for r in results if r.status == STATUS_OK and any(s.flag for s in r.surcharges)]
    no_tariff = Counter(r.flight["Airline"] for r in results if r.status == STATUS_NO_TARIFF)
    other = []
    for carrier, b in _batch_summary(conn).items():
        if b:
            t = f"{CARRIER_PREFIX[carrier]}_Other"
            other += [dict(x, Carrier=carrier) for x in conn.execute(
                f"SELECT * FROM {t} WHERE BatchID = ?", (b["BatchID"],))]
    return render_template("exceptions.html", check=check, ended=ended, no_tariff=no_tariff, other=other,
                           as_of=as_of)


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
    hubs, stations = _stations(conn)
    rows, series = [], []
    if origin and dest:
        tc = {s["Code"]: s["TariffCode"] for s in stations if s.get("TariffCode")}
        o, d = tc.get(origin, origin), tc.get(dest, dest)
        cond = "ApplicableFlights" if carrier == "IndiGo" else "ApplicableFlightNumber"
        table = f"{CARRIER_PREFIX[carrier]}_{product}"
        rows = [dict(r) for r in conn.execute(
            f"SELECT t.*, b.SourceFileName, t.{cond} AS Cond FROM {table} t "
            f"JOIN LoadBatches b ON b.BatchID = t.BatchID "
            f"WHERE t.OriginCode = ? AND t.DestCode = ? AND b.Status = 'OK' "
            f"ORDER BY t.EffectiveDate, t.RateID", (o, d))]
        # one line per distinct condition (route rate, time windows, flight lists...)
        by_cond = {}
        for r in rows:
            label = " ".join((r["Cond"] or "All flights").split())
            if r.get("ApplicableCommodity"):
                label += f" [{r['ApplicableCommodity']}]"
            by_cond.setdefault(label, []).append(r)
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
    t = f"{CARRIER_PREFIX[carrier]}_{product}"
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
    prefix = CARRIER_PREFIX.get(carrier)
    if not prefix:
        abort(404)
    counts = {p: conn.execute(f"SELECT COUNT(*) FROM {prefix}_{p} WHERE BatchID=?", (batch["BatchID"],))
              .fetchone()[0] for p in CARRIER_TABLES[carrier]}
    product = request.args.get("product")
    if product not in counts:
        product = next((p for p in CARRIER_TABLES[carrier] if counts[p]), CARRIER_TABLES[carrier][0])
    origin = (request.args.get("origin") or "").upper()
    dest = (request.args.get("dest") or "").upper()
    sql = f"SELECT * FROM {prefix}_{product} WHERE BatchID = ?"
    params = [batch["BatchID"]]
    if origin:
        sql += " AND OriginCode = ?"
        params.append(origin)
    if dest:
        sql += " AND DestCode = ?"
        params.append(dest)
    limit = 500
    rows = [dict(r) for r in conn.execute(sql + f" ORDER BY OriginCode, DestCode, RateID LIMIT {limit + 1}",
                                          params)]
    truncated = len(rows) > limit
    rows = rows[:limit]
    cond_col = "ApplicableFlights" if carrier == "IndiGo" else "ApplicableFlightNumber"
    if carrier == "IndiGo":
        extra = ["ApplicableCommodity", "SectorSurcharge", "FlightSurcharge", "CommoditySurcharge"]
    else:
        extra = ["ApplicableCommodity", "Surcharge", "SeasonalSurcharge", "Remarks"]
    return render_template("tariffs.html", batches=batches, batch=batch, counts=counts, product=product,
                           rows=rows, truncated=truncated, limit=limit, origin=origin, dest=dest,
                           cond_col=cond_col, extra=extra, carrier=carrier)


@bp.route("/notes")
def notes():
    conn = get_db()
    latest = _batch_summary(conn)
    ai = latest.get("Air India")
    ig = latest.get("IndiGo")
    ai_notes = [dict(r) for r in conn.execute(
        "SELECT * FROM AirIndia_Notes WHERE BatchID=? ORDER BY RowOrder", (ai["BatchID"],))] if ai else []
    ig_notes = [dict(r) for r in conn.execute(
        "SELECT * FROM IndiGo_Footnotes WHERE BatchID=? ORDER BY FootnoteID", (ig["BatchID"],))] if ig else []
    tsp = [dict(r) for r in conn.execute(
        "SELECT * FROM IndiGo_TSPCharges WHERE BatchID=? ORDER BY TSPID", (ig["BatchID"],))] if ig else []
    return render_template("notes.html", ai=ai, ig=ig, ai_notes=ai_notes, ig_notes=ig_notes, tsp=tsp)


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
