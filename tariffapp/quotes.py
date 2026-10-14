"""
Customer rate quotations - the files in data/quotes.

    *.xlsx   a rate quote workbook (e.g. ALLCARGO LOGISTICS LIMITED.xlsx):
             airline charges (minimum freight, AWB, DO, DC, valuation, discount)
             and a per-route quote (tariff, discount, base, terminal & surcharges,
             master and console rate), followed by the quote's terms.
    *.pdf    a special-rates sheet sent with it (e.g. Akasa Special Rates):
             SSP rate (+100 kg) and console rate per route and flight type.

The lines above the customer's name (the issuing company and its address)
and any term naming the issuer are left out: the app shows the quote for the
customer, not who issued it.
"""

import datetime as dt
import hashlib
import os
import re

QUOTE_EXT = (".xlsx", ".xlsm", ".pdf")

_WEF_RE = re.compile(r"w\.?\s*e\.?\s*f\.?\s*[:\-]?\s*(\d{1,2})[-/.](\d{1,2})[-/.](\d{4})", re.I)
_SPECIAL_RE = re.compile(r"^([A-Z]{3})\s+([A-Z]{3})\s+([A-Za-z][A-Za-z ]*?)\s+(Prime|GCR|ALL)\s+(\S+)\s+"
                         r"(\d+(?:\.\d+)?)\+*\s+(\d+(?:\.\d+)?)\s*$", re.I)
_NOTE_NUM_RE = re.compile(r"^(?:note\s*:?\s*)?\d+\s*[.)]\s*", re.I)
# Terms that name the quote's issuer (e.g. who cheques are payable to) are not shown.
_ISSUER_RE = re.compile(r"\bindex\b", re.I)

QUOTE_COLUMNS = ["ORG", "DEST", "AIRLINE", "FLIGHT", "COMMODITY", "N RATE", "Q45", "Q100", "Q250", "Q300", "Q500",
                 "Q1000", "DISC/KG", "DISC%", "BASE+100 RATE", "TERMINAL&SURCHARGES", "MASTER+100", "CONSOLE RATE"]
CHARGE_COLUMNS = {"MINIMUM FREIGHT": "MinFreight", "AWB CHG": "AwbCharge", "DO CHARGE": "DoCharge",
                  "DC CHARGE": "DcCharge", "DRY ICE CHARGE": "DryIceCharge", "VAL CHARGE": "ValCharge",
                  "DISCOUNT%": "DiscPct", "DISCOUNT/KG": "DiscPerKg", "AWB DISCOUNT": "AwbDiscount"}


def airline_code(name):
    """'Indigo' / 'Air India' / 'STAR AIR' / 'Akasa' -> INDIGO / AIRINDIA / STARAIR / AKASA"""
    return re.sub(r"[^A-Z0-9]", "", str(name or "").upper())


def flight_key(text):
    """'6E 0496' / 'QP-1925' -> '6E496'; 'ALL' / 'GCR' / 'Prime' -> 'ALL' / 'GCR' / 'PRIME'."""
    t = re.sub(r"[^A-Z0-9]", "", str(text or "").upper())
    m = re.match(r"^([A-Z0-9]{2})0*(\d{1,4})([A-Z]?)$", t)
    if m and not t.isalpha():
        return f"{m.group(1)}{int(m.group(2))}{m.group(3)}"
    return t or "ALL"


def _key(v):
    return re.sub(r"\s+", "", str(v or "")).upper().rstrip(".")


def _num(v):
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return float(v)
    m = re.search(r"-?\d+(?:\.\d+)?", str(v).replace(",", ""))
    return float(m.group()) if m else None


def _text(v):
    if v is None:
        return None
    if isinstance(v, float) and v == int(v):
        v = int(v)
    s = " ".join(str(v).split())
    return s or None


def _wef(text):
    m = _WEF_RE.search(text or "")
    if not m:
        return None
    d, mth, y = map(int, m.groups())
    try:
        return dt.date(y, mth, d).isoformat()
    except ValueError:
        return None


def _note(text):
    t = _NOTE_NUM_RE.sub("", " ".join(str(text).split()))
    return None if not t or _ISSUER_RE.search(t) else t


def read_quote_workbook(path):
    """-> {customer, effective, charges: [...], rates: [...], notes: [...]}"""
    import openpyxl

    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    try:
        rows = [list(r) for r in wb.worksheets[0].iter_rows(values_only=True)]
    finally:
        wb.close()
    first = [_text(r[0]) if r else None for r in rows]
    out = {"customer": None, "effective": None, "charges": [], "rates": [], "notes": []}

    # The customer is the line just above "Rate Quotation"; anything above it is the issuer.
    for i, t in enumerate(first):
        if t and re.match(r"^rate\s+quot", t, re.I):
            out["customer"] = next((first[j] for j in range(i - 1, -1, -1) if first[j]), None)
            break
    for t in first[:15]:
        out["effective"] = out["effective"] or _wef(t)

    i, n = 0, len(rows)
    while i < n:
        heads = [_key(c) for c in rows[i]]
        if heads and heads[0] == "AIRLINE" and "MINIMUMFREIGHT" in heads:
            cols = {CHARGE_COLUMNS[k]: j for j, h in enumerate(heads)
                    for k in CHARGE_COLUMNS if h == _key(k)}
            i += 1
            while i < n and _text(rows[i][0]):
                r = rows[i]
                out["charges"].append({"Airline": airline_code(r[0]), "AirlineName": _text(r[0]),
                                       **{k: _text(r[j]) if j < len(r) else None for k, j in cols.items()}})
                i += 1
            continue
        if heads[:2] == ["ORG", "DEST"]:
            cols = {k: heads.index(_key(k)) for k in QUOTE_COLUMNS if _key(k) in heads}
            i += 1
            while i < n and re.match(r"^[A-Z]{3}$", str(rows[i][0] or "").strip()):
                r = rows[i]

                def g(k):
                    j = cols.get(k)
                    return r[j] if j is not None and j < len(r) else None
                disc_kg = g("DISC/KG")
                note = None if _num(disc_kg) is not None or disc_kg is None else _text(disc_kg)
                out["rates"].append({
                    "Origin": str(g("ORG")).strip().upper(), "Destination": str(g("DEST")).strip().upper(),
                    "Airline": airline_code(g("AIRLINE")), "Flight": flight_key(g("FLIGHT")),
                    "Commodity": _text(g("COMMODITY")),
                    "RateN": _num(g("N RATE")), "RateQ45": _num(g("Q45")), "RateQ100": _num(g("Q100")),
                    "RateQ250": _num(g("Q250")), "RateQ300": _num(g("Q300")), "RateQ500": _num(g("Q500")),
                    "RateQ1000": _num(g("Q1000")),
                    "DiscPerKg": _num(disc_kg) if note is None else None, "DiscPct": _num(g("DISC%")),
                    "Base100": _num(g("BASE+100 RATE")), "Terminal": _num(g("TERMINAL&SURCHARGES")),
                    "Master100": _num(g("MASTER+100")), "Console": _num(g("CONSOLE RATE")), "Note": note,
                })
                i += 1
            # the quote's terms follow the table
            for r in rows[i:]:
                t = _text(r[0]) if r else None
                if t and (note := _note(t)):
                    out["notes"].append(note)
            break
        i += 1
    return out


def read_special_pdf(path):
    """Special-rates PDF -> {effective, airline, rates: [...], notes: [...]}"""
    import pdfplumber

    with pdfplumber.open(path) as pdf:
        text = "\n".join(page.extract_text() or "" for page in pdf.pages)
    return parse_special_text(text)


def parse_special_text(text):
    out = {"effective": _wef(text), "rates": [], "notes": []}
    in_notes = False
    for line in text.splitlines():
        line = " ".join(line.split())
        m = _SPECIAL_RE.match(line)
        if m:
            o, d, airline, ftype, commodity, ssp, console = m.groups()
            out["rates"].append({"Origin": o.upper(), "Destination": d.upper(), "Airline": airline_code(airline),
                                 "Flight": ftype.upper(), "Commodity": commodity, "Base100": float(ssp),
                                 "Console": float(console)})
            continue
        if re.match(r"^notes?\s*:?\s*$", line, re.I):
            in_notes = True
            continue
        if in_notes and line:
            if re.match(r"^\d+\s*[.)]", line) or not out["notes"]:
                if note := _note(line):
                    out["notes"].append(note)
            elif out["notes"]:
                out["notes"][-1] += " " + line   # wrapped line of the previous note
    return out


def files(folder):
    if not os.path.isdir(folder):
        return []
    return sorted(os.path.join(folder, f) for f in os.listdir(folder)
                  if f.lower().endswith(QUOTE_EXT) and not f.startswith(("~$", ".")))


def folder_digest(folder):
    paths = files(folder)
    if not paths:
        return None
    h = hashlib.sha256()
    for p in paths:
        h.update(os.path.basename(p).encode())
        with open(p, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
    return h.hexdigest()


_RATE_COLS = ["Kind", "Origin", "Destination", "Airline", "Flight", "Commodity", "RateN", "RateQ45", "RateQ100",
              "RateQ250", "RateQ300", "RateQ500", "RateQ1000", "DiscPerKg", "DiscPct", "Base100", "Terminal",
              "Master100", "Console", "Note"]
_CHARGE_COLS = ["Airline", "AirlineName", *CHARGE_COLUMNS.values()]


def import_folder(conn, folder):
    """Replace the stored quotes with the files in `folder`. A special-rates PDF goes with the
    workbook of the same effective date (else the newest). -> list of loaded descriptions."""
    paths = files(folder)
    books = [(p, read_quote_workbook(p)) for p in paths if not p.lower().endswith(".pdf")]
    specials = [(p, read_special_pdf(p)) for p in paths if p.lower().endswith(".pdf")]
    conn.execute("DELETE FROM Quotes")
    loaded, ids = [], []
    for p, q in books:
        cur = conn.execute("INSERT INTO Quotes (SourceFileName, Customer, EffectiveDate) VALUES (?,?,?)",
                           (os.path.basename(p), q["customer"], q["effective"]))
        qid = cur.lastrowid
        ids.append((q["effective"] or "", qid))
        conn.executemany(f"INSERT INTO QuoteRates (QuoteID, {', '.join(_RATE_COLS)}) "
                         f"VALUES (?, {', '.join('?' * len(_RATE_COLS))})",
                         [(qid, "quote", *[r.get(c) for c in _RATE_COLS[1:]]) for r in q["rates"]])
        conn.executemany(f"INSERT INTO QuoteCharges (QuoteID, {', '.join(_CHARGE_COLS)}) "
                         f"VALUES (?, {', '.join('?' * len(_CHARGE_COLS))})",
                         [(qid, *[c.get(k) for k in _CHARGE_COLS]) for c in q["charges"]])
        conn.executemany("INSERT INTO QuoteNotes (QuoteID, Kind, Seq, Text) VALUES (?, 'quote', ?, ?)",
                         [(qid, i, t) for i, t in enumerate(q["notes"], 1)])
        loaded.append(f"{os.path.basename(p)} ({len(q['rates'])} routes)")
    for p, s in specials:
        if not ids:
            break
        qid = next((i for eff, i in ids if eff and eff == s["effective"]), max(ids)[1])
        conn.executemany(f"INSERT INTO QuoteRates (QuoteID, {', '.join(_RATE_COLS)}) "
                         f"VALUES (?, {', '.join('?' * len(_RATE_COLS))})",
                         [(qid, "special", *[r.get(c) for c in _RATE_COLS[1:]]) for r in s["rates"]])
        conn.executemany("INSERT INTO QuoteNotes (QuoteID, Kind, Seq, Text) VALUES (?, 'special', ?, ?)",
                         [(qid, i, t) for i, t in enumerate(s["notes"], 1)])
        loaded.append(f"{os.path.basename(p)} ({len(s['rates'])} special rates)")
    conn.commit()
    return loaded


# ---------------------------------------------------------------------------
# Reading back: the quote in force, and its rate for one flight
# ---------------------------------------------------------------------------

def current_quote(conn, as_of):
    """Newest quote effective on or before `as_of` (else the earliest one)."""
    rows = conn.execute("SELECT QuoteID, SourceFileName, Customer, EffectiveDate FROM Quotes "
                        "ORDER BY COALESCE(EffectiveDate, '') DESC, QuoteID DESC").fetchall()
    if not rows:
        return None
    iso = as_of.isoformat()
    q = next((r for r in rows if not r["EffectiveDate"] or r["EffectiveDate"] <= iso), rows[-1])
    return dict(q)


def route_quote(conn, quote_id, origin, dest):
    """All quote rows for the route, grouped by airline: {airline: {'quote': [...], 'special': [...]}}"""
    out = {}
    for r in conn.execute("SELECT * FROM QuoteRates WHERE QuoteID = ? AND Origin = ? AND Destination = ?",
                          (quote_id, origin, dest)):
        out.setdefault(r["Airline"], {"quote": [], "special": []})[r["Kind"]].append(dict(r))
    return out


def rate_for_flight(route_rows, airline, flight_no, product=None):
    """The quote line that applies to one flight, or None.

    A line for this flight number wins over the route's ALL (or GCR) line. Where the
    quote says the rate is "as per attached special rates", or the special-rates sheet
    lists the route, the special rate is used: its Prime line for a prime flight (the
    quote lists the flight, or the tariff prices it as PRIME), else its GCR line, else ALL.
    """
    rows = route_rows.get(airline)
    if not rows:
        return None
    key = flight_key(flight_no)
    quote = rows["quote"]
    line = (next((r for r in quote if r["Flight"] == key), None)
            or next((r for r in quote if r["Flight"] == "ALL"), None)
            or next((r for r in quote if r["Flight"] in ("GCR", "PRIME")), None))
    # The quote lists an airline's prime flights by number (the rest of the route is ALL / GCR).
    prime = product == "PRIME" or bool(line and line["Flight"] == key)
    special = rows["special"]
    if special:
        by_type = {r["Flight"]: r for r in special}
        pick = (by_type.get("PRIME") if prime else by_type.get("GCR")) or by_type.get("ALL")
        if pick:
            return {**pick, "Special": True, "DiscPerKg": None, "DiscPct": None, "Terminal": None,
                    "Master100": None}
    if line and line["Console"] is None and line["Note"]:
        return {**line, "Special": False}   # special rates promised but not on the sheet: show the note
    return {**line, "Special": False} if line else None


def charges(conn, quote_id, airlines):
    rows = conn.execute("SELECT * FROM QuoteCharges WHERE QuoteID = ?", (quote_id,)).fetchall()
    return [dict(r) for r in rows if r["Airline"] in airlines]


def notes(conn, quote_id, kind="quote"):
    return [r["Text"] for r in conn.execute(
        "SELECT Text FROM QuoteNotes WHERE QuoteID = ? AND Kind = ? ORDER BY Seq", (quote_id, kind))]
