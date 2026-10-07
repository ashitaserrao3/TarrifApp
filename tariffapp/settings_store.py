"""
Write-side helpers for the Settings tables, used by the Streamlit editors
(which hand back the whole edited table at once).

Each save_* validates every row first and raises ValueError with a readable
message if anything is wrong; only then is the table replaced, in one
transaction, so a bad edit never leaves half a table behind.
"""

import datetime as dt

from .schema import CARRIER_TABLES

APPLIES_TO = ["All", "Origin", "Destination", "Route"]


def _text(v):
    if v is None:
        return None
    try:  # pandas NA / NaN
        if v != v:
            return None
    except (TypeError, ValueError):
        return None
    s = str(v).strip()
    return s or None


def _num(v, field, row_no):
    s = _text(v)
    if s is None:
        return None
    try:
        return float(s)
    except ValueError:
        raise ValueError(f"Row {row_no}: {field} must be a number, got '{s}'.") from None


def _date(v, field, row_no):
    if v is None or _text(v) is None:
        return None
    if isinstance(v, dt.datetime):
        return v.date().isoformat()
    if isinstance(v, dt.date):
        return v.isoformat()
    if hasattr(v, "date"):  # pandas Timestamp
        return v.date().isoformat()
    try:
        return dt.date.fromisoformat(str(v)[:10]).isoformat()
    except ValueError:
        raise ValueError(f"Row {row_no}: {field} must be a date (YYYY-MM-DD), got '{v}'.") from None


def _flag(v):
    if isinstance(v, str):
        return 0 if v.strip().upper() in ("", "N", "NO", "FALSE", "0") else 1
    return 1 if v and v == v else 0


def save_network_surcharges(conn, rows):
    clean = []
    for i, r in enumerate(rows, 1):
        if all(_text(r.get(k)) is None for k in ("Carrier", "Station", "RatePerKg", "Reason", "Label")):
            continue  # blank row the editor added
        carrier = _text(r.get("Carrier"))
        if carrier not in CARRIER_TABLES:
            raise ValueError(f"Row {i}: Carrier must be one of {', '.join(CARRIER_TABLES)}.")
        applies = _text(r.get("AppliesTo")) or "All"
        if applies not in APPLIES_TO:
            raise ValueError(f"Row {i}: Applies to must be one of {', '.join(APPLIES_TO)}.")
        station = (_text(r.get("Station")) or "").upper() or None
        if applies != "All" and not station:
            raise ValueError(f"Row {i}: '{applies}' surcharges need a station (or a DEL-COK style route).")
        if applies == "Route" and station and "-" not in station:
            raise ValueError(f"Row {i}: Route surcharges need an ORIGIN-DEST route like DEL-COK.")
        rate = _num(r.get("RatePerKg"), "Rs/kg", i)
        if rate is None:
            raise ValueError(f"Row {i}: Rs/kg is required.")
        vf, vt = _date(r.get("ValidFrom"), "Valid from", i), _date(r.get("ValidTo"), "Valid to", i)
        if vf and vt and vt < vf:
            raise ValueError(f"Row {i}: Valid to is before Valid from.")
        clean.append((carrier, applies, station if applies != "All" else None, rate, vf, vt,
                      _text(r.get("Reason")), _flag(r.get("Active")), _text(r.get("Label"))))
    with conn:
        conn.execute("DELETE FROM NetworkSurcharges")
        conn.executemany(
            "INSERT INTO NetworkSurcharges (Carrier, AppliesTo, Station, RatePerKg, ValidFrom, ValidTo, Reason, "
            "Active, Label) VALUES (?,?,?,?,?,?,?,?,?)", clean)
    return len(clean)


def save_airlines(conn, rows):
    clean, seen = [], set()
    for i, r in enumerate(rows, 1):
        code = (_text(r.get("Code")) or "").upper()
        if not code:
            if any(_text(r.get(k)) for k in ("Name", "AddOnPerKg", "AddOnIncludes")):
                raise ValueError(f"Row {i}: airline Code is required.")
            continue
        if code in seen:
            raise ValueError(f"Row {i}: airline {code} is listed twice.")
        seen.add(code)
        tc = _text(r.get("TariffCarrier"))
        if tc and tc not in CARRIER_TABLES:
            raise ValueError(f"Row {i}: Tariff must be blank or one of {', '.join(CARRIER_TABLES)}.")
        clean.append((code, _text(r.get("Name")) or code, tc, _num(r.get("AddOnPerKg"), "Add-on", i),
                       _text(r.get("AddOnIncludes"))))
    with conn:
        conn.execute("DELETE FROM Airlines")
        conn.executemany("INSERT INTO Airlines (Code, Name, TariffCarrier, AddOnPerKg, AddOnIncludes) "
                         "VALUES (?,?,?,?,?)", clean)
    return len(clean)


def save_stations(conn, rows):
    clean, seen = [], set()
    for i, r in enumerate(rows, 1):
        code = (_text(r.get("Code")) or "").upper()
        if not code:
            if any(_text(r.get(k)) for k in ("Name", "TariffCode")):
                raise ValueError(f"Row {i}: station Code is required.")
            continue
        if code in seen:
            raise ValueError(f"Row {i}: station {code} is listed twice.")
        seen.add(code)
        order = _num(r.get("SortOrder"), "Sort order", i)
        clean.append((code, _text(r.get("Name")), _flag(r.get("IsHub")),
                      None if order is None else int(order), (_text(r.get("TariffCode")) or "").upper() or None))
    with conn:
        conn.execute("DELETE FROM Stations")
        conn.executemany("INSERT INTO Stations (Code, Name, IsHub, SortOrder, TariffCode) VALUES (?,?,?,?,?)",
                         clean)
    return len(clean)
