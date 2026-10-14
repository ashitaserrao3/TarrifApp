"""Queries the dashboard needs. Plain functions over a sqlite3 connection."""

import datetime as dt

from .importers import DAYS
from .linker import TariffEngine
from .schema import CARRIER_TABLES

# Order airlines appear in on the Dashboard (the workbook's Raw Data order); others follow by name.
AIRLINE_ORDER = ["INDIGO", "AIRINDIA", "AKASA", "SPICEJET"]


def stations(conn):
    """(hubs, all_stations) as lists of dicts. Hubs feed the Origin picker."""
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM Stations ORDER BY COALESCE(SortOrder, 999), Code")]
    hubs = [r for r in rows if r["IsHub"]]
    if not hubs:  # nothing marked yet: every origin in the schedule is a hub
        origins = {r[0] for r in conn.execute("SELECT DISTINCT Origin FROM Flights")}
        hubs = [r for r in rows if r["Code"] in origins]
    return hubs, rows


def destinations(conn, origins):
    """Airports with nonstop flights from any of `origins` in the schedule in use, plus the origins
    themselves (in station order)."""
    src = "dgca" if conn.execute("SELECT 1 FROM Flights WHERE Source = 'dgca' LIMIT 1").fetchone() else "workbook"
    marks = ",".join("?" * len(origins))
    served = {r[0] for r in conn.execute(
        f"SELECT DISTINCT Destination FROM Flights WHERE COALESCE(Source, 'workbook') = ? AND Origin IN ({marks})",
        (src, *origins))} | set(origins)
    return [r["Code"] for r in conn.execute("SELECT Code FROM Stations ORDER BY COALESCE(SortOrder, 999), Code")
            if r["Code"] in served]


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


def _state(conn, key):
    r = conn.execute("SELECT Value FROM AppState WHERE Key = ?", (key,)).fetchone()
    return r[0] if r else None


def schedule_info(conn, as_of):
    """Which flight schedule is in use and for which date.

    With DGCA PDFs loaded, flights are taken as they operate on `as_of`. Outside the
    schedule's season (e.g. the summer schedule after 24 Oct) the nearest day the
    schedule covers is used instead, and `expired` / `not_started` says so."""
    has_dgca = conn.execute("SELECT 1 FROM Flights WHERE Source = 'dgca' LIMIT 1").fetchone() is not None
    if not has_dgca:
        n = conn.execute("SELECT COUNT(*) FROM Flights").fetchone()[0]
        return {"source": "workbook", "label": "Schedule workbook", "date": None, "flights": n,
                "expired": False, "not_started": False, "valid_from": None, "valid_to": None}
    vf, vt = _state(conn, "dgca_valid_from") or None, _state(conn, "dgca_valid_to") or None
    day = as_of.isoformat()
    expired, not_started = bool(vt and day > vt), bool(vf and day < vf)
    used = vt if expired else (vf if not_started else day)
    n = conn.execute("SELECT COUNT(DISTINCT Airline || FlightNo || Origin || Destination || DepTime) FROM Flights "
                     "WHERE Source = 'dgca' AND EffectiveFrom <= ? AND EffectiveTo >= ?", (used, used)).fetchone()[0]
    season = _state(conn, "dgca_season") or ""
    year = (vf or day)[:4]
    return {"source": "dgca", "label": f"DGCA {season} {year}".replace("  ", " ").strip(),
            "date": dt.date.fromisoformat(used), "flights": n, "expired": expired, "not_started": not_started,
            "valid_from": vf, "valid_to": vt}


def route_flights(conn, origin, dest, as_of):
    """The flights operating origin -> dest, one row per flight number and departure time,
    with the days of the week it operates on (DGCA rows valid on the schedule date are merged)."""
    info = schedule_info(conn, as_of)
    if info["source"] == "workbook":
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM Flights WHERE Origin=? AND Destination=? ORDER BY FlightID", (origin, dest))]
    else:
        day = info["date"].isoformat()
        merged = {}
        for r in conn.execute(
                "SELECT * FROM Flights WHERE Source = 'dgca' AND Origin=? AND Destination=? "
                "AND EffectiveFrom <= ? AND EffectiveTo >= ? ORDER BY FlightID", (origin, dest, day, day)):
            r = dict(r)
            key = (r["Airline"], r["FlightNo"], r["DepTime"])
            if key in merged:
                for d in DAYS:
                    merged[key][d] = 1 if (merged[key][d] or r[d]) else 0
            else:
                merged[key] = r
        rows = list(merged.values())
    order = {a: i for i, a in enumerate(AIRLINE_ORDER)}
    rows.sort(key=lambda r: (order.get(r["Airline"], len(order)), r["Airline"], r["DepTime"] or "", r["FlightNo"]))
    return rows


def price_route(conn, origin, dest, as_of):
    """-> (engine, [FlightRate], route_status, weekly_flights)"""
    eng = TariffEngine(conn, as_of)
    results = eng.price_all(route_flights(conn, origin, dest, as_of))
    status = "SAME CITY" if origin == dest else ("NO SERVICE" if not results else "ACTIVE")
    return eng, results, status, sum(weekly_frequency(r.flight) for r in results)
