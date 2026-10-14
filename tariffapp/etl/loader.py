"""
Load an Air India / IndiGo rate-list .xlsx into the database.

Ported from the original CargoRates etl.py: same parsing, same per-product
tables, same LoadBatches bookkeeping (a file that's already loaded is
skipped), but talking to the app's SQLite database and returning a result
dict instead of printing.
"""

import datetime as dt
import os
import sqlite3

import openpyxl

from . import parsers
from ..schema import AIRLINE_CARRIER, CARRIER_PREFIX, GENERIC_CARRIERS

_GENERIC_READERS = {"Akasa": parsers.read_akasa, "SpiceJet": parsers.read_spicejet, "Star Air": parsers.read_starair}
_SURCHARGE_COLUMNS = ["BatchID", "Origins", "Destinations", "ExceptOrigins", "Flights", "ExceptFlights",
                      "RatePerKg", "Label", "Reason", "ValidFrom", "ValidTo"]


def _iso(d):
    return d.strftime("%Y-%m-%d 00:00:00") if d else None


def already_loaded(conn, fname):
    return conn.execute(
        "SELECT COUNT(*) FROM LoadBatches WHERE SourceFileName = ? AND Status = 'OK'", (fname,)
    ).fetchone()[0] > 0


def _insert_batch(conn, carrier, fname, eff, rows, status, notes=""):
    now = dt.datetime.now().replace(microsecond=0).isoformat(sep=" ")
    cur = conn.execute(
        "INSERT INTO LoadBatches (Carrier, SourceFileName, EffectiveDate, LoadedAt, RowsLoaded, Status, Notes) "
        "VALUES (?,?,?,?,?,?,?)",
        (carrier, fname, eff, now, rows, status, notes),
    )
    return cur.lastrowid


def _bulk_insert(conn, table, columns, rows):
    if not rows:
        return
    sql = f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({', '.join('?' * len(columns))})"
    conn.executemany(sql, [tuple(r.get(c) for c in columns) for r in rows])


def load_rate_file(conn: sqlite3.Connection, filepath: str, filename: str = None,
                   force: bool = False, effective_date: dt.date = None) -> dict:
    """Returns {'status': ok|skipped|error, 'carrier', 'effective_date', 'rows',
    'warnings': [...], 'message'}."""
    fname = filename or os.path.basename(filepath)
    result = {"file": fname, "status": None, "carrier": None, "effective_date": None,
              "rows": 0, "warnings": [], "message": ""}

    eff_date = effective_date or parsers.parse_effective_date(fname)
    result["effective_date"] = eff_date

    if not force and already_loaded(conn, fname):
        result.update(status="skipped", message="Already loaded - skipped.")
        return result

    if eff_date is None:
        try:
            eff_date = parsers.find_effective_date_in_workbook(filepath)   # e.g. 'EFF 08th June 2026' in a title
        except Exception:  # noqa: BLE001 - unreadable file: reported below
            eff_date = None
        result["effective_date"] = eff_date
    if eff_date is None:
        result["warnings"].append("Could not find an effective date in the filename or the file.")
    eff = _iso(eff_date)

    try:
        wb = openpyxl.load_workbook(filepath, read_only=True)
        carrier = parsers.detect_carrier(fname, wb.sheetnames)
        wb.close()
    except Exception as e:  # noqa: BLE001 - report anything back to the user
        _insert_batch(conn, "UNKNOWN", fname, eff, 0, "ERROR", str(e))
        conn.commit()
        result.update(status="error", message=f"Could not open / detect carrier: {e}")
        return result
    result["carrier"] = carrier

    try:
        if carrier == "Air India":
            data = parsers.read_air_india(filepath)
            total = len(data["rates"]) + len(data["commodity_codes"]) + len(data["notes"])
            batch_id = _insert_batch(conn, carrier, fname, eff, total, "OK")
            _insert_rates(conn, "AirIndia", parsers.AIRINDIA_TABLES, parsers.AIRINDIA_RATE_COLUMNS,
                          data["rates"], batch_id, eff, result)
            for rec in data["commodity_codes"]:
                rec["BatchID"] = batch_id
            _bulk_insert(conn, "AirIndia_CommodityCodes",
                         ["BatchID", "LMSCode", "Description", "iCargoCode"], data["commodity_codes"])
            for rec in data["notes"]:
                rec.update(BatchID=batch_id, EffectiveDate=eff)
            _bulk_insert(conn, "AirIndia_Notes",
                         ["BatchID", "EffectiveDate", "SectionNo", "HeadingText", "DetailText", "RowOrder"],
                         data["notes"])
        elif carrier == "IndiGo":
            data = parsers.read_indigo(filepath)
            total = len(data["rates"]) + len(data["footnotes"]) + len(data["tsp"])
            batch_id = _insert_batch(conn, carrier, fname, eff, total, "OK")
            _insert_rates(conn, "IndiGo", parsers.INDIGO_TABLES, parsers.INDIGO_RATE_COLUMNS,
                          data["rates"], batch_id, eff, result)
            for rec in data["footnotes"]:
                rec.update(BatchID=batch_id, EffectiveDate=eff)
            _bulk_insert(conn, "IndiGo_Footnotes", ["BatchID", "EffectiveDate", "SeqNo", "FootnoteText"],
                         data["footnotes"])
            for rec in data["tsp"]:
                rec.update(BatchID=batch_id, EffectiveDate=eff)
            _bulk_insert(conn, "IndiGo_TSPCharges",
                         ["BatchID", "EffectiveDate", "Station", "TariffCategory", "OutboundRatePerKg",
                          "OutboundMinCharge", "InboundRatePerKg", "InboundMinCharge", "Remarks"],
                         data["tsp"])
        elif carrier in GENERIC_CARRIERS:
            data = _GENERIC_READERS[carrier](filepath)
            total = len(data["rates"]) + len(data["surcharges"])
            batch_id = _insert_batch(conn, carrier, fname, eff, total, "OK")
            _insert_rates(conn, CARRIER_PREFIX[carrier], GENERIC_CARRIERS[carrier], parsers.GENERIC_RATE_COLUMNS,
                          data["rates"], batch_id, eff, result)
            for rec in data["surcharges"]:
                rec["BatchID"] = batch_id
            _bulk_insert(conn, "BatchSurcharges", _SURCHARGE_COLUMNS, data["surcharges"])
        else:
            raise ValueError(f"Unhandled carrier '{carrier}'")
        # The schedule's airline code for this carrier now has a tariff
        for code, c in AIRLINE_CARRIER.items():
            if c == carrier:
                conn.execute("INSERT OR IGNORE INTO Airlines (Code, Name) VALUES (?, ?)", (code, carrier))
                conn.execute("UPDATE Airlines SET TariffCarrier = ? WHERE Code = ? AND TariffCarrier IS NULL",
                             (carrier, code))
        conn.commit()
        result.update(status="ok", rows=total, batch_id=batch_id,
                      message=f"Loaded {total} rows.")
        return result
    except Exception as e:  # noqa: BLE001
        conn.rollback()
        try:
            _insert_batch(conn, carrier, fname, eff, 0, "ERROR", str(e))
            conn.commit()
        except Exception:  # noqa: BLE001
            pass
        result.update(status="error", message=str(e))
        return result


def _insert_rates(conn, prefix, tables, columns, rates, batch_id, eff, result):
    by_table = {t: [] for t in tables}
    for rec in rates:
        rec["BatchID"] = batch_id
        rec["EffectiveDate"] = eff
        by_table[rec["_Table"]].append(rec)
    if by_table["Other"]:
        result["warnings"].append(
            f"{len(by_table['Other'])} row(s) landed in {prefix}_Other (unrecognised category) - worth a look.")
    for suffix, rows in by_table.items():
        _bulk_insert(conn, f"{prefix}_{suffix}", ["BatchID", "EffectiveDate"] + columns, rows)


def delete_batch(conn, batch_id: int):
    """Removes a batch and every row it loaded (rate tables cascade)."""
    conn.execute("DELETE FROM LoadBatches WHERE BatchID = ?", (batch_id,))
    conn.commit()
