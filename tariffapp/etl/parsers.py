"""
parsers.py
----------
Everything that turns one raw rate-list .xlsx (as received from Air India or
IndiGo) into plain Python dicts, ready to be inserted into the database.

No database code lives here on purpose - these functions just read a
workbook and hand back lists of dicts. etl.py does the DB work.
"""

import re
import datetime as dt
import openpyxl


# ============================================================================
# Filename -> effective date
# ============================================================================

_MONTHS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
    "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
}

# Matches things like:
#   "01-Mar-26", "12-Aug-26", "effective12-Aug-26"
#   "01st March 2026", "16th April 2026", "7th May 2026", "01stAug2026", "01st_Oct_2026"
_DATE_RE = re.compile(
    r"(\d{1,2})(?:st|nd|rd|th)?[\s\-_.]*([A-Za-z]{3,9})[\s\-_.,]*(\d{2,4})"
)


def parse_effective_date(filename: str) -> "dt.date | None":
    """Best-effort extraction of the effective date embedded in a filename."""
    for m in _DATE_RE.finditer(filename):
        day_s, mon_s, year_s = m.groups()
        mon = _MONTHS.get(mon_s.lower())
        if not mon:
            continue
        try:
            day = int(day_s)
            year = int(year_s)
        except ValueError:
            continue
        if year < 100:
            year += 2000
        try:
            return dt.date(year, mon, day)
        except ValueError:
            continue
    return None


def find_effective_date_in_workbook(path) -> "dt.date | None":
    """For files whose name carries no date (e.g. Star Air '...W.E.F. 8TH JUNE-UFN'): look for
    'EFF 08th June 2026' / 'EFFECTIVE : 29-Jul-2026' in the first rows of each sheet."""
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    try:
        for ws in wb.worksheets:
            for row in ws.iter_rows(min_row=1, max_row=6, values_only=True):
                for v in row:
                    if isinstance(v, str) and re.search(r"\b(eff|effective|w\.?e\.?f)", v, re.I):
                        d = parse_effective_date(v)
                        if d:
                            return d
    finally:
        wb.close()
    return None


def detect_carrier(filename: str, sheet_names: list) -> str:
    """Air India files have a 'Tariff Sheet'; IndiGo files have 'GCR+LEAN...'; Akasa an 'OCDC' sheet;
    SpiceJet 'PRIME FLIGHTS' + 'VIA FLIGHTS RATES'; Star Air 'GCR &PER PUBLISHED RATES'."""
    lower_sheets = [s.lower() for s in sheet_names]
    fn = filename.lower()
    if any("tariff sheet" in s for s in lower_sheets):
        return "Air India"
    if any("gcr" in s and "lean" in s for s in lower_sheets):
        return "IndiGo"
    if "akasa" in fn or any(s.strip() == "ocdc" for s in lower_sheets):
        return "Akasa"
    if "spicejet" in fn or re.match(r"^sg[\s_]", fn) or (
            any("prime flights" in s for s in lower_sheets) and any("via flights" in s for s in lower_sheets)):
        return "SpiceJet"
    if "star air" in fn or "starair" in fn or any("per published" in s for s in lower_sheets):
        return "Star Air"
    if "air india" in fn or "airindia" in fn:
        return "Air India"
    if "indigo" in fn or "6e" in fn:
        return "IndiGo"
    raise ValueError(f"Could not detect carrier for '{filename}'")


# ============================================================================
# Generic header-row helpers
# ============================================================================

def _norm(text) -> str:
    if text is None:
        return ""
    return re.sub(r"\s+", " ", str(text)).strip().lower()


# canonical field -> set of header spellings seen across cycles
_HEADER_ALIASES = {
    "Region": {"region"},
    "OriginCode": {"origin code", "org"},
    "DestCode": {"destination code", "dest"},
    "Sector": {"sector", "lane"},
    "ApplicableFlightNumber": {"applicable flight number", "flight no"},
    "RateCategory": {"prime/gcr/ lean/scr", "prime/gcr/lean/scr", "type"},
    "ApplicableCommodity": {"applicable commodity", "commodity number"},
    "MinCharge": {"min"},
    "RateN": {"n", "n rate"},
    "RateQ45": {"q45", "+45"},
    "RateQ100": {"q100", "+100"},
    "RateQ300": {"q300", "+300"},
    "RateQ500": {"q500", "+500"},
    "RateQ1000": {"q1000", "+1000"},
    "Surcharge": {"surcharge"},
    "SeasonalSurcharge": {"seasonal surcharge"},
    "Remarks": {"remarks"},
    "ApplicableFlights": {"applicable flights"},
    "SectorSurcharge": {"sector surcharge"},
    "FlightSurcharge": {"flight surcharge"},
    "CommoditySurcharge": {"commodity surcharge"},
}


def _find_header_row(ws, must_have, max_scan=15):
    """Scan the first `max_scan` rows for one that contains most of the
    tokens in `must_have` (already-normalised strings). Returns
    (row_index, {canonical_field: col_index})."""
    best_row, best_score, best_map = None, 0, {}
    for r in range(1, max_scan + 1):
        row_vals = [ws.cell(row=r, column=c).value for c in range(1, ws.max_column + 1)]
        norm_vals = [_norm(v) for v in row_vals]
        score = sum(1 for tok in must_have if tok in norm_vals)
        if score > best_score:
            colmap = {}
            for c, nv in enumerate(norm_vals, start=1):
                if not nv:
                    continue
                for canon, aliases in _HEADER_ALIASES.items():
                    if nv in aliases and canon not in colmap:
                        colmap[canon] = c
            best_row, best_score, best_map = r, score, colmap
    if best_row is None or best_score == 0:
        raise ValueError(f"Could not find a header row in sheet '{ws.title}'")
    return best_row, best_map


def _cell(ws, row, colmap, field):
    col = colmap.get(field)
    if col is None:
        return None
    v = ws.cell(row=row, column=col).value
    if isinstance(v, str):
        v = v.strip()
        if v == "":
            return None
    return v


def _num(v):
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip()
    if not s:
        return None
    s = re.sub(r"[^\d.\-]", "", s)
    try:
        return float(s)
    except ValueError:
        return None


RATE_FIELDS = [
    "Region", "OriginCode", "DestCode", "Sector", "ApplicableFlightNumber",
    "RateCategory", "ApplicableCommodity", "MinCharge", "RateN", "RateQ45",
    "RateQ100", "RateQ300", "RateQ500", "RateQ1000", "Surcharge",
    "SeasonalSurcharge", "Remarks", "ApplicableFlights", "SectorSurcharge",
    "FlightSurcharge", "CommoditySurcharge",
]

_NUMERIC_FIELDS = {"MinCharge", "RateN", "RateQ45", "RateQ100", "RateQ300",
                    "RateQ500", "RateQ1000"}

# Column sets actually present on each carrier's rate table (see schema_*.sql).
# _extract_rate_sheet() always fills every key in RATE_FIELDS, but each table
# only has the subset of columns relevant to that carrier's sheets - etl.py
# uses these lists (not RATE_FIELDS) when it builds the INSERT statement.
AIRINDIA_RATE_COLUMNS = [
    "Region", "OriginCode", "DestCode", "Sector", "ApplicableFlightNumber",
    "RateCategory", "ApplicableCommodity", "MinCharge", "RateN", "RateQ45",
    "RateQ100", "RateQ300", "RateQ500", "RateQ1000", "Surcharge",
    "SeasonalSurcharge", "Remarks", "SourceSheet",
]

INDIGO_RATE_COLUMNS = [
    "Region", "OriginCode", "DestCode", "RateCategory", "MinCharge", "RateN",
    "RateQ45", "RateQ100", "RateQ300", "RateQ500", "RateQ1000",
    "ApplicableFlights", "ApplicableCommodity", "SectorSurcharge",
    "FlightSurcharge", "CommoditySurcharge", "SourceSheet",
]


# ============================================================================
# Category classification - which per-product table a rate row belongs in.
#
# The raw category text drifts wording release to release (IndiGo's
# Priority/XPS product alone has shown up as 3 different phrasings), so
# instead of trusting the exact text, each row is classified from the
# sheet it came from PLUS keyword matching on the raw text. Anything that
# doesn't confidently match falls into "Other" rather than being dropped
# or silently mis-filed - check AirIndia_Other / IndiGo_Other after a
# load; they should normally be empty or near-empty. If a genuinely new
# product shows up in a future file, it'll land in Other and you'll see
# it - add a rule for it here (and a matching table in schema_*.sql) once
# you know its real name.
# ============================================================================

AIRINDIA_TABLES = ["GCR", "PRIME", "SCR", "HEA", "Other"]
INDIGO_TABLES = ["GCR", "LEAN", "SCR", "HEA", "XPS", "DGR", "RFL", "RLI", "Other"]


def classify_airindia(source_sheet, raw_category):
    s = (source_sheet or "").lower()
    c = (raw_category or "").upper()
    if "hea" in s:  # the "HEA rates on WB" sheet - sheet context wins over
        return "HEA"  # whatever's in its Prime/GCR/Lean/SCR column
    if "PRIME" in c:
        return "PRIME"
    if "GCR" in c:
        return "GCR"
    if "SCR" in c:
        return "SCR"
    return "Other"


def classify_indigo(source_sheet, raw_category):
    s = (source_sheet or "").lower()
    c = (raw_category or "").upper()
    if "gcr" in s and "lean" in s:  # the combined GCR+LEAN+SCR+DGR sheet
        if "GCR" in c:
            return "GCR"
        if "LEAN" in c:
            return "LEAN"
        if "SCR" in c:
            return "SCR"
        if "DGR" in c:
            return "DGR"
        return "Other"
    if "hea" in s:
        return "HEA"
    if "xps" in s:  # covers "6E XPS & RFL rates" / "6E XPS Rates" / "6E XPS & RLI Rates"
        if "RLI" in c or "LI-ION" in c:
            return "RLI"
        if "XPS" in c or "PRIORITY" in c or "FLAMMABLE" in c:
            return "XPS"
        return "Other"
    if "dgr" in s:  # covers "DGR" / "DGR & RFL Rates"
        if "RFL" in c and "DGR" not in c:
            return "RFL"
        if "DGR" in c or c == "":  # a handful of rows have a blank Type cell
            return "DGR"           # but sit in a DGR-named sheet - trust the sheet
        return "Other"
    return "Other"


def _extract_rate_sheet(ws, source_sheet_name):
    header_row, colmap = _find_header_row(
        ws, must_have={"org", "origin code"} | {"dest", "destination code"}
    )
    if "OriginCode" not in colmap or "DestCode" not in colmap:
        raise ValueError(f"Sheet '{ws.title}' has no recognisable ORG/DEST columns")

    rows = []
    for r in range(header_row + 1, ws.max_row + 1):
        origin = _cell(ws, r, colmap, "OriginCode")
        dest = _cell(ws, r, colmap, "DestCode")
        if origin is None and dest is None:
            continue
        rec = {f: _cell(ws, r, colmap, f) for f in RATE_FIELDS}
        for f in _NUMERIC_FIELDS:
            rec[f] = _num(rec[f])
        rec["SourceSheet"] = source_sheet_name
        rows.append(rec)
    return rows


# ============================================================================
# Air India workbook
# ============================================================================

def read_air_india(path):
    wb = openpyxl.load_workbook(path, data_only=True)
    out = {"rates": [], "commodity_codes": [], "notes": []}

    if "Tariff Sheet" in wb.sheetnames:
        out["rates"].extend(_extract_rate_sheet(wb["Tariff Sheet"], "Tariff Sheet"))

    for name in wb.sheetnames:
        if "hea" in name.lower() and "wb" in name.lower():
            out["rates"].extend(_extract_rate_sheet(wb[name], name))

    if "Commodity Codes" in wb.sheetnames:
        ws = wb["Commodity Codes"]
        header_row, colmap = _find_header_row(ws, must_have={"lms  commodites", "description"}, max_scan=10)
        # header names have odd whitespace ("LMS \nCommodites"); fall back to
        # fixed layout (col B=LMS code, C=Description, D=iCargo) if the
        # generic matcher didn't find them.
        for r in range(4, ws.max_row + 1):
            lms = ws.cell(row=r, column=2).value
            desc = ws.cell(row=r, column=3).value
            icargo = ws.cell(row=r, column=4).value
            if lms is None and desc is None:
                continue
            out["commodity_codes"].append({
                "LMSCode": str(lms).strip() if lms is not None else None,
                "Description": str(desc).strip() if desc is not None else None,
                "iCargoCode": str(icargo).strip() if icargo is not None else None,
            })

    if "Notation" in wb.sheetnames:
        ws = wb["Notation"]
        current_section = None
        for r in range(5, ws.max_row + 1):
            c3 = ws.cell(row=r, column=3).value  # section number
            c4 = ws.cell(row=r, column=4).value  # heading / detail
            c5 = ws.cell(row=r, column=5).value  # sub-detail
            if c3 is not None:
                current_section = str(c3).strip()
                out["notes"].append({
                    "SectionNo": current_section,
                    "HeadingText": str(c4).strip() if c4 is not None else None,
                    "DetailText": None,
                    "RowOrder": r,
                })
            elif c4 is not None or c5 is not None:
                detail = c4 if c4 is not None else c5
                out["notes"].append({
                    "SectionNo": current_section,
                    "HeadingText": None,
                    "DetailText": str(detail).strip(),
                    "RowOrder": r,
                })

    for rec in out["rates"]:
        rec["_Table"] = classify_airindia(rec.get("SourceSheet"), rec.get("RateCategory"))

    return out


# ============================================================================
# IndiGo workbook
# ============================================================================

def _match_sheet(sheet_names, *, must_contain_all=(), must_contain_any=(), exclude=()):
    for name in sheet_names:
        low = name.lower()
        if any(x.lower() in low for x in exclude):
            continue
        if must_contain_all and not all(x.lower() in low for x in must_contain_all):
            continue
        if must_contain_any and not any(x.lower() in low for x in must_contain_any):
            continue
        return name
    return None


def read_indigo(path):
    wb = openpyxl.load_workbook(path, data_only=True)
    names = wb.sheetnames
    out = {"rates": [], "footnotes": [], "tsp": []}

    combined = _match_sheet(names, must_contain_all=("gcr", "lean"))
    if combined:
        out["rates"].extend(_extract_rate_sheet(wb[combined], combined))

    hea = _match_sheet(names, must_contain_all=("hea",))
    if hea:
        out["rates"].extend(_extract_rate_sheet(wb[hea], hea))

    xps = _match_sheet(names, must_contain_all=("xps",))
    if xps:
        out["rates"].extend(_extract_rate_sheet(wb[xps], xps))

    dgr = _match_sheet(names, must_contain_all=("dgr",), exclude=("gcr",))
    if dgr:
        out["rates"].extend(_extract_rate_sheet(wb[dgr], dgr))

    footnotes_sheet = _match_sheet(names, must_contain_all=("footnote",))
    if footnotes_sheet:
        ws = wb[footnotes_sheet]
        for r in range(1, ws.max_row + 1):
            seq = ws.cell(row=r, column=1).value
            text = ws.cell(row=r, column=2).value
            if text is None:
                continue
            text = str(text).strip()
            if not text or text.lower().startswith("indigo cargo footnotes"):
                continue
            out["footnotes"].append({
                "SeqNo": str(seq).strip() if seq is not None else None,
                "FootnoteText": text,
            })

    if "Sheet1" in names:
        ws = wb["Sheet1"]
        last_station = None
        for r in range(5, ws.max_row + 1):
            station = ws.cell(row=r, column=1).value
            category = ws.cell(row=r, column=2).value
            ob_rate = ws.cell(row=r, column=3).value
            ob_min = ws.cell(row=r, column=4).value
            ib_rate = ws.cell(row=r, column=5).value
            ib_min = ws.cell(row=r, column=6).value
            remarks = ws.cell(row=r, column=7).value
            if station is not None:
                last_station = str(station).strip()
            if category is None:
                continue
            out["tsp"].append({
                "Station": last_station,
                "TariffCategory": str(category).strip(),
                "OutboundRatePerKg": _num(ob_rate),
                "OutboundMinCharge": _num(ob_min),
                "InboundRatePerKg": _num(ib_rate),
                "InboundMinCharge": _num(ib_min),
                "Remarks": str(remarks).strip() if remarks is not None else None,
            })

    for rec in out["rates"]:
        rec["_Table"] = classify_indigo(rec.get("SourceSheet"), rec.get("RateCategory"))

    return out


# ============================================================================
# Akasa, SpiceJet and Star Air - one generic table reader, keyed by header text
# ============================================================================

AKASA_TABLES = ["GCR", "PRIME", "Other"]
SPICEJET_TABLES = ["GCR", "PRIME", "LEAN", "Other"]
STARAIR_TABLES = ["GCR", "Other"]

# Rate-table columns for these carriers (IndiGo's layout plus a 250 kg break and remarks).
GENERIC_RATE_COLUMNS = [
    "Region", "OriginCode", "DestCode", "RateCategory", "MinCharge", "RateN",
    "RateQ45", "RateQ100", "RateQ250", "RateQ300", "RateQ500", "RateQ1000",
    "ApplicableFlights", "ApplicableCommodity", "SectorSurcharge",
    "FlightSurcharge", "CommoditySurcharge", "Remarks", "SourceSheet",
]

_GENERIC_HEADERS = {   # header text with spaces removed, lower case
    "OriginCode": {"origin", "org", "origincode"},
    "DestCode": {"destination", "dest", "destinationcode"},
    "RateCategory": {"type", "category"},
    "MinCharge": {"min", "minimum"},
    "RateN": {"n", "nrate", "normal", "normalrate"},
    "RateQ45": {"+45", "45+", "45", "q45"},
    "RateQ100": {"+100", "100+", "100", "q100"},
    "RateQ250": {"+250", "250+", "250", "q250"},
    "RateQ300": {"+300", "300+", "300", "q300"},
    "RateQ500": {"+500", "500+", "500", "q500"},
    "RateQ1000": {"+1000", "1000+", "1000", "q1000"},
    "ApplicableFlights": {"flightno", "fltno", "flight", "flights", "applicableflights"},
    "ApplicableCommodity": {"commodity"},
    "Surcharge": {"surcharge"},
    "Remarks": {"remark", "remarks"},
}
_GENERIC_NUMERIC = {"MinCharge", "RateN", "RateQ45", "RateQ100", "RateQ250", "RateQ300", "RateQ500", "RateQ1000"}
_CODE3 = re.compile(r"^[A-Z]{3}$")


def _generic_rows(ws, sheet_label):
    """Rows of a rate sheet whose header row has Origin/ORG and Destination/DEST columns."""
    header_row, colmap = None, {}
    for r, row in enumerate(ws.iter_rows(min_row=1, max_row=12, values_only=True), start=1):
        keys = [re.sub(r"\s+", "", str(v)).lower() if v is not None else "" for v in row]
        cm = {}
        for c, k in enumerate(keys):
            for field, aliases in _GENERIC_HEADERS.items():
                if k in aliases and field not in cm:
                    cm[field] = c
        if "OriginCode" in cm and "DestCode" in cm and "RateN" in cm:
            header_row, colmap = r, cm
            break
    if header_row is None:
        return []
    out = []
    for row in ws.iter_rows(min_row=header_row + 1, values_only=True):
        def get(f):
            c = colmap.get(f)
            v = row[c] if c is not None and c < len(row) else None
            if isinstance(v, str):
                v = " ".join(v.split()) or None
            return v
        o, d = (str(get("OriginCode") or "").strip().upper(), str(get("DestCode") or "").strip().upper())
        if not (_CODE3.match(o) and _CODE3.match(d)):
            continue
        rec = {f: None for f in GENERIC_RATE_COLUMNS}
        rec.update(OriginCode=o, DestCode=d, SourceSheet=sheet_label)
        for f in ("RateCategory", "ApplicableFlights", "ApplicableCommodity", "Remarks"):
            v = get(f)
            rec[f] = str(v) if v is not None else None
        for f in _GENERIC_NUMERIC:
            rec[f] = _num(get(f))
        if rec["RateN"] is None and rec["RateQ45"] is None:
            continue
        rec["_Surcharge"] = get("Surcharge")
        out.append(rec)
    return out


def _all_word(v):
    return v is None or str(v).strip().lower() in ("", "all", "all flights", "gcr", "general")


def read_akasa(path):
    wb = openpyxl.load_workbook(path, data_only=True)
    out = {"rates": [], "surcharges": []}
    for ws in wb.worksheets:
        if ws.title.strip().lower() in ("ocdc", "surcharges", "terms & conditions"):
            continue
        for rec in _generic_rows(ws, ws.title):
            cat = (rec["RateCategory"] or "").strip().lower()
            rec["_Table"] = "PRIME" if cat == "prime" else ("GCR" if cat in ("gcr", "") else "Other")
            if rec["_Table"] == "GCR" and _all_word(rec["ApplicableFlights"]):
                rec["ApplicableFlights"] = None
            out["rates"].append(rec)
    sur = next((n for n in wb.sheetnames if n.strip().lower() == "surcharges"), None)
    if sur:
        out["surcharges"] = _akasa_surcharges(wb[sur])
    return out


def _akasa_surcharges(ws):
    """Akasa's surcharge list mixes general and commodity-only charges. Keep the ones that
    apply to general cargo: no commodity named, and the 'flight' column holding flight
    numbers, an exclusion, or nothing (not a commodity code list like 'PER / PEF')."""
    rows = list(ws.iter_rows(values_only=True))
    hdr = next((i for i, r in enumerate(rows) if r and any(str(v).strip().upper() == "ORIGIN" for v in r if v)), None)
    if hdr is None:
        return []
    names = [re.sub(r"\s+", " ", str(v)).strip().upper() if v else "" for v in rows[hdr]]
    col = lambda *keys: next((i for i, n in enumerate(names) if any(n.startswith(k) for k in keys)), None)  # noqa: E731
    c_o, c_d, c_f, c_c = col("ORIGIN"), col("DEST"), col("FLIGHT"), col("COMMODITY")
    c_from, c_to, c_rate = col("FROM"), col("TO DATE"), col("RATE")
    flight_re = re.compile(r"(?<![A-Z0-9])QP\s*-?\s*(\d{3,4})", re.I)
    out = []
    for r in rows[hdr + 1:]:
        r = list(r) + [None] * 14
        m = re.search(r"\d+(\.\d+)?", str(r[c_rate] or "")) if c_rate is not None else None
        rate = float(m.group()) if m else None   # 'Rs.10/-' -> 10
        if not rate:
            continue
        commodity = str(r[c_c] or "").strip() if c_c is not None else ""
        ftxt = " ".join(str(r[c_f] or "").split())
        if re.search(r"excl", commodity, re.I):        # 'EXCLUDING QP1804' typed in the commodity column
            ftxt, commodity = (ftxt + " " + commodity).strip(), ""
        if commodity and commodity.lower() not in ("all", "general"):
            continue                                   # commodity-only surcharge
        flights = [f"QP{int(n)}" for n in flight_re.findall(ftxt)]
        excl = bool(re.search(r"excl", ftxt, re.I))
        if ftxt and not flights and not excl and ftxt.lower() != "all":
            continue                                   # e.g. 'PER / PEF', '(Special Cargo i.e. AVI,...)'
        o = str(r[c_o] or "").strip().upper()
        d = str(r[c_d] or "").strip().upper()
        rec = {"Origins": None if o in ("", "ALL") else o, "Destinations": None if d in ("", "ALL") else d,
               "ExceptOrigins": None, "Flights": None, "ExceptFlights": None, "RatePerKg": rate,
               "Label": "Akasa surcharge", "Reason": f"Akasa surcharge Rs {rate:g}/kg"
                                                    + (f" ({ftxt})" if ftxt else ""),
               "ValidFrom": _iso_date(r[c_from]) if c_from is not None else None,
               "ValidTo": _iso_date(r[c_to]) if c_to is not None else None}
        if excl:
            codes = [c for c in re.findall(r"\b[A-Z]{3}\b", ftxt.upper()) if c not in ("ALL", "EXC")]
            if flights:
                rec["ExceptFlights"] = ",".join(flights)
            elif codes:
                rec["ExceptOrigins"] = ",".join(codes)    # e.g. 'BOM excluded', 'EXCLUDED CCU to IXZ'
        elif flights:
            rec["Flights"] = ",".join(flights)
        out.append(rec)
    return out


def _iso_date(v):
    if isinstance(v, dt.datetime):
        return v.date().isoformat()
    if isinstance(v, dt.date):
        return v.isoformat()
    return None  # 'UFN' (until further notice) and blanks


def read_spicejet(path):
    wb = openpyxl.load_workbook(path, data_only=True)
    out = {"rates": [], "surcharges": []}
    for ws in wb.worksheets:
        t = ws.title.strip().lower()
        if "prime" in t:
            table = "PRIME"
        elif "lean" in t:
            table = "LEAN"
        elif t == "gcr":
            table = "GCR"
        else:
            continue  # SCR (city names, commodity-specific), VIA (connecting), handling and CUT sheets
        for rec in _generic_rows(ws, ws.title):
            rec["_Table"] = table
            if _all_word(rec["ApplicableCommodity"]):
                rec["ApplicableCommodity"] = None
            if table == "GCR" and _all_word(rec["ApplicableFlights"]):
                rec["ApplicableFlights"] = None
            amt = _num(rec.pop("_Surcharge", None))
            if amt:
                key = "FlightSurcharge" if rec["ApplicableFlights"] else "SectorSurcharge"
                rec[key] = f"INR {amt:g} / KG"
            out["rates"].append(rec)
    for ws in wb.worksheets:
        if "handling" not in ws.title.lower():
            continue
        for row in ws.iter_rows(values_only=True):
            cells = [str(v).strip() for v in row if v is not None and str(v).strip()]
            if len(cells) < 2:
                continue
            name, rate = cells[0].lower(), _num(re.search(r"\d+(\.\d+)?", cells[1]).group()) \
                if re.search(r"\d", cells[1]) else None
            if not rate or "surcharge" not in name:
                continue
            label = "Fuel" if "fuel" in name else ("Security" if "security" in name else
                                                   ("Commercial" if "commercial" in name else "Surcharge"))
            origins = None
            rest = " ".join(cells[2:])
            m = re.search(r"from\s*:?\s*([A-Z ,]+)", rest, re.I)
            if m:
                origins = ",".join(re.findall(r"\b[A-Z]{3}\b", m.group(1).upper())) or None
            out["surcharges"].append({
                "Origins": origins, "Destinations": None, "ExceptOrigins": None, "Flights": None,
                "ExceptFlights": None, "RatePerKg": rate, "Label": label,
                "Reason": f"SpiceJet {cells[0]} INR {rate:g}/kg" + (f" (from {origins})" if origins else ""),
                "ValidFrom": None, "ValidTo": None})
    return out


def read_starair(path):
    wb = openpyxl.load_workbook(path, data_only=True)
    out = {"rates": [], "surcharges": []}
    for ws in wb.worksheets:
        t = ws.title.lower()
        if "published" in t or "gcr" in t:
            for rec in _generic_rows(ws, ws.title):
                cat = (rec["RateCategory"] or "").strip().upper()
                rec["_Table"] = "GCR" if cat in ("GEN", "GCR", "") else "Other"
                rec["ApplicableCommodity"] = None if rec["_Table"] == "GCR" else rec["ApplicableCommodity"]
                rec.pop("_Surcharge", None)
                out["rates"].append(rec)
        elif "foot" in t:
            for row in ws.iter_rows(values_only=True):
                cells = [str(v).strip() for v in row if v is not None and str(v).strip()]
                if len(cells) < 2 or "surcharge" not in cells[0].lower():
                    continue
                m = re.search(r"rs\.?\s*(\d+(\.\d+)?)", cells[1], re.I)
                if not m or not re.search(r"all\s+sectors", cells[1], re.I):
                    continue
                rate = float(m.group(1))
                label = "Fuel" if "fuel" in cells[0].lower() else ("Sector" if "sector" in cells[0].lower()
                                                                   else "Surcharge")
                out["surcharges"].append({
                    "Origins": None, "Destinations": None, "ExceptOrigins": None, "Flights": None,
                    "ExceptFlights": None, "RatePerKg": rate, "Label": label,
                    "Reason": f"Star Air {cells[0]} Rs {rate:g}/kg on all sectors",
                    "ValidFrom": None, "ValidTo": None})
    return out
