"""
Importers for data that lives outside the rate files:

* import_schedule_workbook - the flight-schedule workbook
  (ATC_AOC_Flight_Schedule_*.xlsx). Reads 'Raw Data' (flights), 'Lists'
  (station names, hub list), 'Add-on Rates' and 'Network Surcharges'.
  Any of those sheets may be missing; only what's present is imported.

* import_legacy_db - copies every batch out of an existing CargoRates
  database (the SQLite demo, or the Access .accdb on Windows via pyodbc)
  so you keep your full rate history.
"""

import datetime as dt
import os
import re
import sqlite3

import openpyxl

from .schema import rate_table_names

DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

# Schedule code -> code used in the airlines' rate files, where they differ.
KNOWN_TARIFF_CODES = {"TVM": "TRV"}


def _norm(v):
    return re.sub(r"\s+", " ", str(v)).strip().lower() if v is not None else ""


def _s(v):
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def norm_time(v):
    """Excel gives us '17:55', a datetime.time, a fraction of a day, or junk
    like '0'. Return 'HH:MM' when it's a real time, else the raw text."""
    if v is None:
        return None
    if isinstance(v, dt.datetime):
        return v.strftime("%H:%M")
    if isinstance(v, dt.time):
        return v.strftime("%H:%M")
    if isinstance(v, float) and 0 < v < 1:
        mins = round(v * 24 * 60)
        return f"{mins // 60:02d}:{mins % 60:02d}"
    s = str(v).strip()
    m = re.fullmatch(r"(\d{1,2})[:.](\d{2})(?::\d{2})?", s)
    if m and int(m.group(1)) < 24 and int(m.group(2)) < 60:
        return f"{int(m.group(1)):02d}:{m.group(2)}"
    return s or None


def _date(v):
    if v is None or v == "":
        return None
    if isinstance(v, dt.datetime):
        return v.date().isoformat()
    if isinstance(v, dt.date):
        return v.isoformat()
    s = str(v).strip()
    for fmt in ("%Y-%m-%d", "%Y-%m-%d %H:%M:%S", "%d-%m-%Y", "%d/%m/%Y", "%d-%b-%y", "%d-%b-%Y"):
        try:
            return dt.datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            pass
    return None


def _num(v):
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        m = re.search(r"-?\d+(?:\.\d+)?", str(v))
        return float(m.group()) if m else None


def _header_map(row, wanted):
    """wanted: {key: [aliases]} -> {key: col_index}"""
    out = {}
    normed = [_norm(c) for c in row]
    for key, aliases in wanted.items():
        for i, h in enumerate(normed):
            if h in aliases or any(h.startswith(a) for a in aliases):
                out[key] = i
                break
    return out


_FLIGHT_COLS = {
    "Origin": ["origin", "org"],
    "Destination": ["destination", "dest"],
    "Airline": ["airline", "carrier"],
    "FlightNo": ["flight no", "flight number", "flight"],
    "DepTime": ["dep time", "etd", "departure"],
    **{d: [d.lower()] for d in DAYS},
}


def _find_flight_sheet(wb):
    if "Raw Data" in wb.sheetnames:
        return wb["Raw Data"]
    for ws in wb.worksheets:
        first = next(ws.iter_rows(min_row=1, max_row=1, values_only=True), ())
        hm = _header_map(first, _FLIGHT_COLS)
        if {"Origin", "Destination", "Airline", "FlightNo"} <= hm.keys():
            return ws
    return None


def import_schedule_workbook(conn: sqlite3.Connection, path: str, filename: str = None) -> dict:
    fname = filename or os.path.basename(path)
    wb = openpyxl.load_workbook(path, data_only=True, read_only=True)
    summary = {"file": fname, "flights": 0, "stations": 0, "airlines": 0, "surcharges": 0,
               "skipped_no_service": 0, "warnings": []}

    ws = _find_flight_sheet(wb)
    if ws is None:
        raise ValueError("No flight sheet found - expected a 'Raw Data' sheet with "
                         "Origin / Destination / Airline / Flight No / Dep Time / Mon..Sun columns.")
    rows = ws.iter_rows(values_only=True)
    hm = _header_map(next(rows), _FLIGHT_COLS)
    missing = {"Origin", "Destination", "Airline", "FlightNo"} - hm.keys()
    if missing:
        raise ValueError(f"Flight sheet '{ws.title}' is missing columns: {', '.join(sorted(missing))}")

    def get(r, k):
        i = hm.get(k)
        return r[i] if i is not None and i < len(r) else None

    flights, seen_codes, route_stations = [], set(), set()
    for r in rows:
        o, d, a = _s(get(r, "Origin")), _s(get(r, "Destination")), _s(get(r, "Airline"))
        if not o or not d:
            continue
        o, d = o.upper(), d.upper()
        route_stations.update([o, d])
        fno = _s(get(r, "FlightNo"))
        if not a or a.upper() == "NO_SERVICE" or not fno:
            summary["skipped_no_service"] += 1
            continue
        a = a.upper()
        seen_codes.add(a)
        days = [1 if _num(get(r, day)) else 0 for day in DAYS]
        flights.append((o, d, a, fno.replace(" ", "").upper(), norm_time(get(r, "DepTime")), *days))

    if not flights:
        raise ValueError("The flight sheet has no flights in it.")

    # Only the workbook's own flights are replaced; flights from DGCA schedule PDFs stay.
    conn.execute("DELETE FROM Flights WHERE COALESCE(Source, 'workbook') = 'workbook'")
    conn.executemany(
        "INSERT INTO Flights (Origin, Destination, Airline, FlightNo, DepTime, Mon, Tue, Wed, Thu, Fri, Sat, Sun, "
        "Source) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,'workbook')", flights)
    summary["flights"] = len(flights)

    # Airlines seen in the schedule but not known yet -> add without a tariff link
    for code in seen_codes:
        conn.execute("INSERT OR IGNORE INTO Airlines (Code, Name) VALUES (?, ?)", (code, code.title()))

    # ---- Lists: station names + hubs -------------------------------------
    if "Lists" in wb.sheetnames:
        lw = wb["Lists"]
        names, hubs, dests = {}, [], []
        for i, r in enumerate(lw.iter_rows(min_row=2, values_only=True)):
            r = list(r) + [None] * 6
            if _s(r[0]):
                names[_s(r[0]).upper()] = _s(r[1])
            if _s(r[3]):
                hubs.append(_s(r[3]).upper())
            if _s(r[5]):
                dests.append(_s(r[5]).upper())
        order = {c: i for i, c in enumerate(dict.fromkeys(dests + list(names)))}
        for code in set(names) | set(hubs) | set(dests) | route_stations:
            conn.execute(
                "INSERT INTO Stations (Code, Name, IsHub, SortOrder) VALUES (?,?,?,?) "
                "ON CONFLICT(Code) DO UPDATE SET Name=COALESCE(excluded.Name, Stations.Name), "
                "IsHub=excluded.IsHub, SortOrder=excluded.SortOrder",
                (code, names.get(code), 1 if code in hubs else 0, order.get(code, 999)))
        summary["stations"] = len(set(names) | route_stations)
    else:
        origins = {f[0] for f in flights}
        for code in route_stations:
            conn.execute("INSERT INTO Stations (Code, IsHub) VALUES (?, ?) "
                         "ON CONFLICT(Code) DO UPDATE SET IsHub = MAX(Stations.IsHub, excluded.IsHub)",
                         (code, 1 if code in origins else 0))
        summary["stations"] = len(route_stations)

    # ---- Add-on Rates ----------------------------------------------------
    if "Add-on Rates" in wb.sheetnames:
        for r in wb["Add-on Rates"].iter_rows(min_row=2, values_only=True):
            r = list(r) + [None] * 4
            code = _s(r[0])
            if not code or " " in code or len(code) > 20:
                continue  # skip the help text under the table
            conn.execute(
                "INSERT INTO Airlines (Code, Name, AddOnPerKg, AddOnIncludes) VALUES (?,?,?,?) "
                "ON CONFLICT(Code) DO UPDATE SET Name=COALESCE(excluded.Name, Airlines.Name), "
                "AddOnPerKg=excluded.AddOnPerKg, AddOnIncludes=excluded.AddOnIncludes",
                (code.upper(), _s(r[1]), _num(r[2]),
                 None if _s(r[3]) == "not set" else _s(r[3])))
            summary["airlines"] += 1

    # ---- Network Surcharges ----------------------------------------------
    if "Network Surcharges" in wb.sheetnames:
        new = []
        for r in wb["Network Surcharges"].iter_rows(min_row=2, values_only=True):
            r = list(r) + [None] * 9
            carrier, applies, rate = _s(r[0]), _s(r[1]), _num(r[3])
            if not carrier or not applies or rate is None:
                continue
            carrier = "Air India" if "air india" in carrier.lower() else (
                "IndiGo" if "indigo" in carrier.lower() else carrier)
            new.append((carrier, applies.title(), (_s(r[2]) or "").upper() or None, rate,
                        _date(r[4]), _date(r[5]), _s(r[6]),
                        0 if (_s(r[7]) or "Y").upper().startswith("N") else 1, _s(r[8])))
        if new:
            conn.execute("DELETE FROM NetworkSurcharges")
            conn.executemany(
                "INSERT INTO NetworkSurcharges (Carrier, AppliesTo, Station, RatePerKg, ValidFrom, ValidTo, "
                "Reason, Active, Label) VALUES (?,?,?,?,?,?,?,?,?)", new)
            summary["surcharges"] = len(new)

    for code, tcode in KNOWN_TARIFF_CODES.items():
        conn.execute("UPDATE Stations SET TariffCode = ? WHERE Code = ? AND TariffCode IS NULL", (tcode, code))

    conn.execute(
        "INSERT INTO ScheduleImports (SourceFileName, ImportedAt, FlightCount, Notes) VALUES (?,?,?,?)",
        (fname, dt.datetime.now().replace(microsecond=0).isoformat(sep=" "), len(flights),
         f"{summary['stations']} stations, {summary['airlines']} add-on rows, {summary['surcharges']} surcharges"))
    conn.commit()
    wb.close()
    return summary


# ---------------------------------------------------------------------------
# Legacy CargoRates database
# ---------------------------------------------------------------------------

_SUPPORT_TABLES = ["AirIndia_CommodityCodes", "AirIndia_Notes", "IndiGo_Footnotes", "IndiGo_TSPCharges"]


def _legacy_connect(path):
    if path.lower().endswith((".accdb", ".mdb")):
        import pyodbc  # Windows only
        return pyodbc.connect(r"DRIVER={Microsoft Access Driver (*.mdb, *.accdb)};" + f"DBQ={path};")
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def _iso_dt(v):
    if isinstance(v, dt.datetime):
        return v.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(v, dt.date):
        return v.strftime("%Y-%m-%d 00:00:00")
    return v


def import_legacy_db(conn: sqlite3.Connection, legacy_path: str, log=print) -> dict:
    src = _legacy_connect(legacy_path)
    scur = src.cursor()
    scur.execute("SELECT BatchID, Carrier, SourceFileName, EffectiveDate, LoadedAt, RowsLoaded, Status, Notes "
                 "FROM LoadBatches WHERE Status = 'OK' ORDER BY EffectiveDate, BatchID")
    batches = scur.fetchall()
    target_cols = {t: [r[1] for r in conn.execute(f"PRAGMA table_info({t})")]
                   for t in rate_table_names() + _SUPPORT_TABLES}
    stats = {"batches": 0, "skipped": 0, "rows": 0}

    for b in batches:
        old_id, carrier, fname = b[0], b[1], b[2]
        if conn.execute("SELECT 1 FROM LoadBatches WHERE SourceFileName=? AND Status='OK'", (fname,)).fetchone():
            stats["skipped"] += 1
            continue
        new_id = conn.execute(
            "INSERT INTO LoadBatches (Carrier, SourceFileName, EffectiveDate, LoadedAt, RowsLoaded, Status, Notes) "
            "VALUES (?,?,?,?,?,?,?)",
            (carrier, fname, _iso_dt(b[3]), _iso_dt(b[4]), b[5], b[6], b[7])).lastrowid
        prefix = "AirIndia" if carrier == "Air India" else "IndiGo"
        n = 0
        for table, cols in target_cols.items():
            if not table.startswith(prefix):
                continue
            try:
                scur.execute(f"SELECT * FROM {table} WHERE BatchID = ?", (old_id,))
            except Exception:  # noqa: BLE001 - table missing in older databases
                continue
            src_cols = [d[0] for d in scur.description]
            keep = [c for c in src_cols if c in cols and c not in ("RateID", "NoteID", "FootnoteID",
                                                                   "TSPID", "CommodityCodeID")]
            rows = scur.fetchall()
            if not rows:
                continue
            idx = [src_cols.index(c) for c in keep]
            bi = keep.index("BatchID")
            data = []
            for r in rows:
                vals = [_iso_dt(r[i]) for i in idx]
                vals[bi] = new_id
                data.append(vals)
            conn.executemany(f"INSERT INTO {table} ({', '.join(keep)}) VALUES ({', '.join('?' * len(keep))})",
                             data)
            n += len(data)
        conn.commit()
        stats["batches"] += 1
        stats["rows"] += n
        log(f"  OK  {carrier:10s} {fname}  ({n} rows)")
    src.close()
    return stats
