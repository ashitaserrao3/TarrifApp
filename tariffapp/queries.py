"""
Read-side queries shared by the Flask views and the Streamlit app.
Plain functions over a sqlite3 connection - no web framework here.
"""

from collections import Counter

from .importers import DAYS
from .linker import (STATUS_CHECK, STATUS_NO_RATE, STATUS_NO_TARIFF, STATUS_OK, TariffEngine,
                     flights_for_route)
from .schema import CARRIER_PREFIX, CARRIER_TABLES

CONDITION_COL = {"IndiGo": "ApplicableFlights", "Air India": "ApplicableFlightNumber"}
EXTRA_COLS = {
    "IndiGo": ["ApplicableCommodity", "SectorSurcharge", "FlightSurcharge", "CommoditySurcharge"],
    "Air India": ["ApplicableCommodity", "Surcharge", "SeasonalSurcharge", "Remarks"],
}


def stations(conn):
    """(hubs, all_stations) as lists of dicts. Hubs feed the Origin picker."""
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM Stations ORDER BY COALESCE(SortOrder, 999), Code")]
    hubs = [r for r in rows if r["IsHub"]]
    if not hubs:  # nothing marked yet: every origin in the schedule is a hub
        origins = {r[0] for r in conn.execute("SELECT DISTINCT Origin FROM Flights")}
        hubs = [r for r in rows if r["Code"] in origins]
    return hubs, rows


def latest_batches(conn):
    out = {}
    for carrier in CARRIER_TABLES:
        r = conn.execute(
            "SELECT * FROM LoadBatches WHERE Carrier=? AND Status='OK' ORDER BY EffectiveDate DESC, BatchID DESC "
            "LIMIT 1", (carrier,)).fetchone()
        out[carrier] = dict(r) if r else None
    return out


def weekly_frequency(flight):
    return sum(int(flight[d] or 0) for d in DAYS)


def price_route(conn, origin, dest, as_of):
    """-> (engine, [FlightRate], route_status, weekly_flights)"""
    eng = TariffEngine(conn, as_of)
    results = eng.price_all(flights_for_route(conn, origin, dest))
    status = "SAME CITY" if origin == dest else ("NO SERVICE" if not results else "ACTIVE")
    return eng, results, status, sum(weekly_frequency(r.flight) for r in results)


def all_priced(conn, as_of):
    flights = [dict(r) for r in conn.execute(
        "SELECT * FROM Flights ORDER BY Origin, Destination, DepTime, FlightNo")]
    return TariffEngine(conn, as_of).price_all(flights)


FLIGHT_EXPORT_HEADER = [
    "Origin", "Destination", "Airline", "Flight No", "Dep Time", *DAYS, "Weekly Freq", "Product",
    "Effective Date", "Rate File", "Min", "N", "45+", "100+", "300+", "500+", "1000+", "Status",
    "Tariff Condition", "Match Note", "Surcharge Rs/kg", "Surcharge Summary", "Add-on Rs/kg",
    "CPKG 45+", "CPKG 100+"]


def flight_export_row(r):
    """One FlightRate as a flat list matching FLIGHT_EXPORT_HEADER."""
    fl, rt = r.flight, r.rates
    return [fl["Origin"], fl["Destination"], fl["Airline"], fl["FlightNo"], fl["DepTime"],
            *[fl[d] for d in DAYS], weekly_frequency(fl), r.product or "",
            r.effective_date or "", r.source_file or "", rt["Min"], rt["N"], rt["45"], rt["100"],
            rt["300"], rt["500"], rt["1000"], r.status,
            r.condition.text if r.condition and r.condition.text else "", r.match_note,
            r.surcharge_total if r.rate_row else "", r.surcharge_summary, r.addon or "",
            r.cpkg(45)[0] or "", r.cpkg(100)[0] or ""]


def lane_history(conn, carrier, product, origin, dest):
    """Every rate row ever loaded for one carrier/product/lane, oldest first.
    Station codes are translated to the tariff's codes (TVM -> TRV)."""
    alias = {r[0]: r[1] for r in conn.execute(
        "SELECT Code, TariffCode FROM Stations WHERE TariffCode IS NOT NULL AND TariffCode <> ''")}
    o, d = alias.get(origin, origin), alias.get(dest, dest)
    table = f"{CARRIER_PREFIX[carrier]}_{product}"
    return [dict(r) for r in conn.execute(
        f"SELECT t.*, b.SourceFileName, t.{CONDITION_COL[carrier]} AS Cond FROM {table} t "
        f"JOIN LoadBatches b ON b.BatchID = t.BatchID "
        f"WHERE t.OriginCode = ? AND t.DestCode = ? AND b.Status = 'OK' "
        f"ORDER BY t.EffectiveDate, t.RateID", (o, d))]


def history_label(row):
    label = " ".join((row.get("Cond") or "All flights").split())
    if row.get("ApplicableCommodity"):
        label += f" [{row['ApplicableCommodity']}]"
    return label


def batch_product_counts(conn, batch):
    prefix = CARRIER_PREFIX[batch["Carrier"]]
    return {p: conn.execute(f"SELECT COUNT(*) FROM {prefix}_{p} WHERE BatchID=?", (batch["BatchID"],))
            .fetchone()[0] for p in CARRIER_TABLES[batch["Carrier"]]}


def tariff_rows(conn, batch, product, origin=None, dest=None, limit=None):
    prefix = CARRIER_PREFIX[batch["Carrier"]]
    if product not in CARRIER_TABLES[batch["Carrier"]]:
        raise ValueError(product)
    sql = f"SELECT * FROM {prefix}_{product} WHERE BatchID = ?"
    params = [batch["BatchID"]]
    if origin:
        sql += " AND OriginCode = ?"
        params.append(origin)
    if dest:
        sql += " AND DestCode = ?"
        params.append(dest)
    sql += " ORDER BY OriginCode, DestCode, RateID"
    if limit:
        sql += f" LIMIT {int(limit)}"
    return [dict(r) for r in conn.execute(sql, params)]


def exceptions(conn, as_of):
    results = all_priced(conn, as_of)
    other = []
    for carrier, b in latest_batches(conn).items():
        if b:
            other += [dict(x, Carrier=carrier) for x in conn.execute(
                f"SELECT * FROM {CARRIER_PREFIX[carrier]}_Other WHERE BatchID = ?", (b["BatchID"],))]
    return {
        "check": [r for r in results if r.status in (STATUS_CHECK, STATUS_NO_RATE)],
        "ended": [r for r in results if r.status == STATUS_OK and any(s.flag for s in r.surcharges)],
        "no_tariff": Counter(r.flight["Airline"] for r in results if r.status == STATUS_NO_TARIFF),
        "other": other,
    }


def notes(conn):
    latest = latest_batches(conn)
    ai, ig = latest.get("Air India"), latest.get("IndiGo")
    return {
        "ai": ai, "ig": ig,
        "ai_notes": [dict(r) for r in conn.execute(
            "SELECT * FROM AirIndia_Notes WHERE BatchID=? ORDER BY RowOrder", (ai["BatchID"],))] if ai else [],
        "ig_notes": [dict(r) for r in conn.execute(
            "SELECT * FROM IndiGo_Footnotes WHERE BatchID=? ORDER BY FootnoteID", (ig["BatchID"],))] if ig else [],
        "tsp": [dict(r) for r in conn.execute(
            "SELECT * FROM IndiGo_TSPCharges WHERE BatchID=? ORDER BY TSPID", (ig["BatchID"],))] if ig else [],
    }
