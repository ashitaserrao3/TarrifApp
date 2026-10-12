"""Queries the dashboard needs. Plain functions over a sqlite3 connection."""

from .importers import DAYS
from .linker import TariffEngine, flights_for_route
from .schema import CARRIER_TABLES


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
