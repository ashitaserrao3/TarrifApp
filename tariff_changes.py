"""
What a new airline tariff file changes, line by line, compared with the tariff in use.

A line is one rate row: route, rate table (GCR, PRIME...), rate category, the
flights it applies to and the commodity. Its values are the minimum charge, the
weight-break rates, the surcharges and the remarks.
"""

import datetime as dt
import os

import openpyxl

from tariffapp.etl import loader, parsers

VALUES = [("MinCharge", "Min"), ("RateN", "N"), ("RateQ45", "45+"), ("RateQ100", "100+"), ("RateQ250", "250+"),
          ("RateQ300", "300+"), ("RateQ500", "500+"), ("RateQ1000", "1000+"), ("Surcharge", "Surcharge"),
          ("SeasonalSurcharge", "Seasonal surcharge"), ("SectorSurcharge", "Sector surcharge"),
          ("FlightSurcharge", "Flight surcharge"), ("CommoditySurcharge", "Commodity surcharge"),
          ("Remarks", "Remarks")]


def _text(v):
    return " ".join(str(v).split()).upper() if v not in (None, "") else ""


def line_key(row):
    flights = row.get("ApplicableFlightNumber") or row.get("ApplicableFlights")
    return (_text(row.get("OriginCode")), _text(row.get("DestCode")), _text(row.get("_Table")),
            _text(row.get("RateCategory")), _text(flights), _text(row.get("ApplicableCommodity")))


def read_tariff(path, name=None):
    """(carrier, effective date or None, {line: row}) for an airline tariff file."""
    name = name or os.path.basename(path)
    wb = openpyxl.load_workbook(path, read_only=True)
    try:
        carrier = parsers.detect_carrier(name, wb.sheetnames)
    finally:
        wb.close()
    eff = parsers.parse_effective_date(name)
    if eff is None:
        try:
            eff = parsers.find_effective_date_in_workbook(path)
        except Exception:  # noqa: BLE001 - no date found: the caller says so
            eff = None
    if carrier == "Air India":
        rows = parsers.read_air_india(path)["rates"]
    elif carrier == "IndiGo":
        rows = parsers.read_indigo(path)["rates"]
    else:
        rows = loader._GENERIC_READERS[carrier](path)["rates"]
    lines = {}
    for row in rows:
        key = line_key(row)
        n = 1
        while key + (n,) in lines:  # the same line twice in one file: keep both
            n += 1
        lines[key + (n,)] = row
    return carrier, eff, lines


def current_lines(files):
    """The lines in use from an airline's current files [(effective date, {line: row})]: the newest file wins
    for every line it has, older files fill in the rest (as the price engine does)."""
    out = {}
    for _, lines in sorted(files, key=lambda f: f[0] or dt.date.min):
        out.update(lines)
    return out


def _value(v):
    if isinstance(v, float) and v.is_integer():
        return int(v)
    return v if v not in ("",) else None


def compare(old, new):
    """{'changed': [(line, row, [(label, old, new)])], 'added': [(line, row)], 'missing': [(line, row)],
    'same': count} for the new file's lines against the lines in use."""
    out = {"changed": [], "added": [], "missing": [], "same": 0}
    for key, row in new.items():
        before = old.get(key)
        if before is None:
            out["added"].append((key, row))
            continue
        diffs = [(label, _value(before.get(field)), _value(row.get(field))) for field, label in VALUES
                 if _text(before.get(field)) != _text(row.get(field))]
        if diffs:
            out["changed"].append((key, row, diffs))
        else:
            out["same"] += 1
    out["missing"] = [(key, row) for key, row in old.items() if key not in new]
    return out


def describe(key):
    """'DEL→CJB · GCR · flights 6E 232' for a line."""
    o, d, table, category, flights, commodity = key[:6]
    parts = [f"{o}→{d}", category or table]
    if flights:
        parts.append(f"flights {flights.title() if flights.isalpha() else flights}")
    if commodity:
        parts.append(commodity.title())
    return " · ".join(parts)
