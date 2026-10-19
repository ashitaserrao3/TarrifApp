"""
The extra rate sources the Flight Schedule prices with:

* Allcargo rate quotation (Index Global, .xlsx): one row per origin, destination,
  airline and flight (a flight number, ALL, or a product such as GCR). Its
  `Terminal&Surcharges` column is added to the airline's own tariff rate
  (not for Akasa, see below).
* Akasa's OCDC sheet (in the Akasa tariff): station charges. Akasa flights add
  the origin's outbound + destination's inbound general-cargo ₹/kg charges.
* Akasa special rates (.pdf): a Direct AWB rate for +100 kg on a few Akasa
  routes, for Prime flights, GCR flights or ALL flights.
"""

import re

import openpyxl
import pdfplumber

# Schedule code for stations the rate sheets spell differently
STATION_ALIASES = {"TRV": "TVM"}


def airline_code(name):
    """'Air India' -> 'AIRINDIA', 'STAR AIR' -> 'STARAIR': the codes the schedule uses."""
    return re.sub(r"[^A-Z0-9]", "", str(name or "").upper())


def station(code):
    code = str(code or "").strip().upper()
    return STATION_ALIASES.get(code, code)


def flight_key(text):
    """'AI 1721' -> 'AI1721', '6E 0496' -> '6E496'; anything else (ALL, GCR, Prime) upper-cased."""
    s = str(text or "").strip().upper()
    m = re.fullmatch(r"([A-Z0-9]{2})[\s-]*0*(\d+)", s)
    return f"{m.group(1)}{m.group(2)}" if m else s


def _num(v):
    if isinstance(v, (int, float)):
        return float(v)
    m = re.match(r"\s*(\d+(?:\.\d+)?)", str(v or ""))
    return float(m.group(1)) if m else None


def _wef(texts):
    for t in texts:
        m = re.search(r"w\.?\s*e\.?\s*f\.?\s*([0-9]{1,2}[-./][0-9]{1,2}[-./][0-9]{2,4})", str(t or ""), re.I)
        if m:
            return m.group(1)
    return ""


def _header(row):
    return [re.sub(r"[^a-z0-9+&]", "", str(v or "").lower()) for v in row]


def _is_quote_header(row):
    h = _header(row)
    return "org" in h and "flight" in h and any("terminal" in x for x in h)


def is_quote(source):
    """True for an Allcargo-style quotation: an ORG / Flight table with a Terminal & Surcharges column."""
    wb = openpyxl.load_workbook(source, read_only=True, data_only=True)
    try:
        return any(_is_quote_header(r) for ws in wb.worksheets for r in ws.iter_rows(max_row=40, values_only=True))
    finally:
        wb.close()


def read_quote(source):
    """{'wef': '01-10-2026', 'rows': {(org, dest, airline, flight): {'ts': float|None, 'note': str}}}"""
    wb = openpyxl.load_workbook(source, read_only=True, data_only=True)
    try:
        ws = next(ws for ws in wb.worksheets
                  if any(_is_quote_header(r) for r in ws.iter_rows(max_row=40, values_only=True)))
        rows = ws.iter_rows(values_only=True)
        top = []
        for row in rows:
            h = _header(row)
            if _is_quote_header(row):
                col = {"org": h.index("org"), "dest": h.index("dest"), "airline": h.index("airline"),
                       "flight": h.index("flight"), "ts": next(i for i, x in enumerate(h) if "terminal" in x)}
                note = next((i for i, x in enumerate(h) if x.startswith("disckg")), None)
                break
            top.append(row[0])
        out = {}
        for row in rows:
            o, d, a = (row[col[k]] for k in ("org", "dest", "airline"))
            if not (isinstance(o, str) and len(o.strip()) == 3 and d and a):
                continue
            key = (station(o), station(d), airline_code(a), flight_key(row[col["flight"]]))
            ts = row[col["ts"]]
            out[key] = {"ts": float(ts) if isinstance(ts, (int, float)) else None,
                        "note": str(row[note]).strip() if note is not None and row[note] else ""}
        return {"wef": _wef(top), "rows": out}
    finally:
        wb.close()


def read_special(source):
    """{'wef': ..., 'rows': {(org, dest, airline, 'PRIME'|'GCR'|'ALL'): {'ssp': float, 'console': float}}}"""
    with pdfplumber.open(source) as pdf:
        text = "\n".join(p.extract_text() or "" for p in pdf.pages)
        tables = [t for p in pdf.pages for t in p.extract_tables()]
    out = {}
    for table in tables:
        for row in table:
            cells = [str(c or "").strip() for c in row]
            if len(cells) < 7 or len(cells[0]) != 3 or not cells[0].isalpha() or cells[0].upper() == "ORG":
                continue
            o, d, a, flight = cells[0], cells[1], cells[2], cells[3]
            ssp, console = _num(cells[5]), _num(cells[6])
            if ssp is None:
                continue
            out[(station(o), station(d), airline_code(a), flight_key(flight))] = {"ssp": ssp, "console": console}
    return {"wef": _wef([text]), "rows": out}


# OCDC categories that aren't general-cargo per-kg terminal charges
_NOT_GENERAL = ("special", "dgr", "avi", "live animal", "ice checklist", "valuable", "demurrage", "demmurrage",
                "storage")


def _rate(v):
    m = re.search(r"(\d+(?:\.\d+)?)", str(v or ""))
    return float(m.group(1)) if m else None


def read_ocdc(source):
    """Akasa's station charges from the tariff's OCDC sheet: the general-cargo ₹/kg charges (X-ray, screening,
    certification, unitization, TSP, handling) per station, outbound and inbound. Special-cargo, DGR, AVI,
    ICE, valuables and demurrage/storage rows are left out; minimum charges are ignored.
    -> {station: {'out': [(charge, ₹/kg)], 'in': [(charge, ₹/kg)]}}"""
    wb = openpyxl.load_workbook(source, read_only=True, data_only=True)
    try:
        ws = next((ws for ws in wb.worksheets if ws.title.strip().upper() == "OCDC"), None)
        if ws is None:
            return {}
        out, stn = {}, None
        for row in ws.iter_rows(values_only=True):
            row = list(row) + [None] * 6
            first = str(row[0] or "").strip().upper()
            if len(first) == 3 and first.isalpha():
                stn = station(first)
            elif first:
                stn = None  # a section title or header row
            cat = re.sub(r"\s+", " ", str(row[1] or "")).strip()
            low = cat.lower()
            perishable_only = ("perishable" in low or "per cargo" in low) and "general" not in low
            if (not stn or not cat or perishable_only or any(w in low for w in _NOT_GENERAL)
                    or low.startswith(("general cargo", "category"))):
                continue
            charges = out.setdefault(stn, {"out": [], "in": []})
            for side, col in (("out", 2), ("in", 4)):
                rate = _rate(row[col])
                if rate:
                    charges[side].append((cat, rate))
        return out
    finally:
        wb.close()


def ocdc_addon(ocdc, flight):
    """Akasa's terminal-charge add-on for a flight: the origin's outbound + the destination's inbound
    general-cargo ₹/kg charges from the OCDC sheet. -> (₹/kg, description) or (None, None)"""
    o, d = station(flight["Origin"]), station(flight["Destination"])
    if not ocdc or (o not in ocdc and d not in ocdc):
        return None, None
    out = ocdc.get(o, {}).get("out", [])
    inb = ocdc.get(d, {}).get("in", [])
    total = round(sum(r for _, r in out) + sum(r for _, r in inb), 2)
    return total, f"{o} outbound {sum(r for _, r in out):g} + {d} inbound {sum(r for _, r in inb):g}"


def quote_row(quote, flight, product, with_number=False):
    """The Allcargo row for a schedule flight, matched by flight number: its own flight row, else its
    product's row (e.g. GCR), else the route's ALL row. With with_number, rows without a Terminal &
    Surcharges number are skipped. -> (row, key) or (None, None)"""
    if not quote:
        return None, None
    o, d, a = station(flight["Origin"]), station(flight["Destination"]), flight["Airline"]
    for k in (flight_key(flight["FlightNo"]), (product or "").upper(), "ALL"):
        row = quote["rows"].get((o, d, a, k))
        if row and (row["ts"] is not None or not with_number):
            return row, k
    return None, None


def special_rate(special, flight, product):
    """The special Direct AWB rate (+100) for a flight the special-rates file covers: the route's ALL row,
    else the row for the flight's product in the airline's tariff (Prime or GCR).
    -> (special row, 'ALL'|'PRIME'|'GCR') or (None, None)"""
    if not special:
        return None, None
    o, d, a = station(flight["Origin"]), station(flight["Destination"]), flight["Airline"]
    for k in ("ALL", "PRIME" if (product or "").upper() == "PRIME" else "GCR"):
        sp = special["rows"].get((o, d, a, k))
        if sp:
            return sp, k
    return None, None
