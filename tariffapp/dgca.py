"""
Read DGCA "Approved Schedule" PDFs (one per airline, published every summer
and winter season) into flight rows the app can use.

Each PDF is an "Airport Movement Report": for every airport (a section headed
by the city name, e.g. "Ahmedabad") it lists each flight that arrives
(Arrival From + Arrival Time) or departs (Departure To + Departure Time),
its days of operation (1 = Monday ... 7 = Sunday) and the dates the row is
valid. A flight often has several rows for different date ranges.

The PDFs name each airport section by city, not by code, so the code is
worked out from the flights themselves: a flight departing section S for D
shows up in D's section as "Arrival From <code of S>".

Reading a large PDF takes a minute or two, so parse_folder() saves the result
as schedule.csv next to the PDFs and reuses it until the PDFs change.
"""

import csv
import datetime as dt
import hashlib
import os
import re
from collections import Counter, defaultdict

# Flight-number prefix -> airline code used in the app (and its display name)
AIRLINES = {
    "6E": ("INDIGO", "IndiGo"),
    "AI": ("AIRINDIA", "Air India / Air India Express"),
    "IX": ("AIRINDIA", "Air India / Air India Express"),
    "QP": ("AKASA", "Akasa Air"),
    "SG": ("SPICEJET", "SpiceJet"),
    "S5": ("STARAIR", "Star Air"),
    "9I": ("ALLIANCEAIR", "Alliance Air"),
    "I7": ("FLY91", "Fly91"),
    "IC": ("ALLIANCEAIR", "Alliance Air"),
}

CSV_NAME = "schedule.csv"
CSV_COLUMNS = ["Origin", "Destination", "Airline", "FlightNo", "DepTime", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat",
               "Sun", "EffectiveFrom", "EffectiveTo", "Aircraft", "Operator", "Season", "OriginName", "DestName"]
DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

_HEADER_RE = re.compile(r"Approved\s+(Summer|Winter)\s+Schedule\s+Domestic\s+Operator\s*-\s*(.+)", re.I)
_TIME_RE = re.compile(r"^\d{1,2}:\d{2}$")
_CODE_RE = re.compile(r"^[A-Z]{3}$")


def _clean(v):
    return " ".join(str(v).split()) if v is not None else ""


def _date(v):
    try:
        return dt.datetime.strptime(_clean(v), "%d/%m/%Y").date().isoformat()
    except ValueError:
        return None


def parse_pdf(path):
    """-> (meta, rows). rows are raw arrival/departure records with their section name."""
    import pdfplumber  # imported here: only needed when PDFs are (re)read

    meta = {"file": os.path.basename(path), "season": None, "operator": None}
    rows, section = [], None
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            if meta["operator"] is None:
                m = _HEADER_RE.search(page.extract_text() or "")
                if m:
                    meta["season"], meta["operator"] = m.group(1).title(), _clean(m.group(2))
            for table in page.extract_tables():
                for cells in table:
                    cells = [_clean(c) for c in cells] + [""] * 11
                    first = cells[0]
                    if not first or first.lower().startswith("sl"):
                        continue
                    if not first.isdigit():          # a section heading: the airport's city name
                        if not any(cells[1:11]):
                            section = first
                        continue
                    flight = cells[1].replace(" ", "").upper()
                    if not section or not re.match(r"^[A-Z0-9]{2}\d+[A-Z]?$", flight):
                        continue
                    rows.append({
                        "section": section, "flight": flight, "operator_code": cells[2],
                        "aircraft": cells[3].replace(" ", "") if "-" in cells[3] else cells[3],
                        "freq": re.sub(r"\D", "", cells[4]),
                        "arr_from": cells[5].upper(), "arr_time": cells[6],
                        "dep_to": cells[7].upper(), "dep_time": cells[8],
                        "eff_from": _date(cells[9]), "eff_to": _date(cells[10]),
                    })
    return meta, rows


def infer_station_codes(rows):
    """Work out each section's airport code from the flights that link sections.
    -> {section name: code}

    A flight F leaving section S for D appears in D's section as "Arrival From
    <code of S>". F may also stop elsewhere (A -> S -> D -> ...), so its other
    arrivals name other airports. Two exclusions remove those: the code cannot be
    D (where F is going), and it cannot be an airport F arrives at S from. The
    reverse holds for flights arriving at S. Votes are then turned into a
    one-to-one assignment, strongest first, so no two cities share a code."""
    arr = defaultdict(list)   # flight -> [(section, from code)]
    dep = defaultdict(list)   # flight -> [(section, to code)]
    for r in rows:
        if _CODE_RE.match(r["arr_from"]):
            arr[r["flight"]].append((r["section"], r["arr_from"]))
        if _CODE_RE.match(r["dep_to"]):
            dep[r["flight"]].append((r["section"], r["dep_to"]))

    votes = defaultdict(Counter)
    for flight in set(arr) | set(dep):
        a, d = arr.get(flight, []), dep.get(flight, [])
        for sec in {s for s, _ in a} | {s for s, _ in d}:
            came_from = {c for s, c in a if s == sec}   # airports F arrives here from
            going_to = {c for s, c in d if s == sec}    # airports F leaves here for
            own = set()
            own |= {c for s, c in a if s != sec}        # elsewhere, "arrived from" this airport (or another stop)
            own |= {c for s, c in d if s != sec}        # elsewhere, "departs to" this airport (or another stop)
            for code in own - came_from - going_to:
                votes[sec][code] += 1

    pairs = sorted(((n, sec, code) for sec, c in votes.items() for code, n in c.items()), reverse=True)
    codes, taken = {}, set()
    for n, sec, code in pairs:
        if sec not in codes and code not in taken:
            codes[sec] = code
            taken.add(code)
    return codes


def build_flights(parsed):
    """parsed: [(meta, rows)] for every PDF -> list of departure dicts (CSV_COLUMNS)."""
    all_rows = [r for _, rows in parsed for r in rows]
    codes = infer_station_codes(all_rows)
    names = {code: sec for sec, code in codes.items()}
    out = []
    for meta, rows in parsed:
        for r in rows:
            if not (_CODE_RE.match(r["dep_to"]) and _TIME_RE.match(r["dep_time"])):
                continue  # arrival rows - the same flight is listed as a departure at its origin
            origin = codes.get(r["section"])
            if not origin or origin == r["dep_to"]:
                continue
            airline = AIRLINES.get(r["flight"][:2], (r["flight"][:2], None))[0]
            h, m = r["dep_time"].split(":")
            days = {d: (1 if str(i + 1) in r["freq"] else 0) for i, d in enumerate(DAYS)}
            out.append({
                "Origin": origin, "Destination": r["dep_to"], "Airline": airline, "FlightNo": r["flight"],
                "DepTime": f"{int(h):02d}:{m}", **days, "EffectiveFrom": r["eff_from"], "EffectiveTo": r["eff_to"],
                "Aircraft": r["aircraft"], "Operator": meta["operator"], "Season": meta["season"],
                "OriginName": r["section"], "DestName": names.get(r["dep_to"], ""),
            })
    return out


def pdf_digest(paths):
    h = hashlib.sha256()
    for p in sorted(paths, key=os.path.basename):
        h.update(os.path.basename(p).encode())
        with open(p, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
    return h.hexdigest()


def read_csv(path):
    """-> (digest the CSV was built from, rows)"""
    with open(path, newline="", encoding="utf-8") as f:
        first = f.readline()
        digest = first.split(":", 1)[1].strip() if first.startswith("# pdf-sha256:") else None
        if digest is None:
            f.seek(0)
        return digest, list(csv.DictReader(f))


def write_csv(path, digest, flights):
    with open(path, "w", newline="", encoding="utf-8") as f:
        f.write(f"# pdf-sha256: {digest}\n")
        w = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        w.writeheader()
        w.writerows(flights)


def parse_folder(folder, log=lambda *_: None):
    """Flights from every DGCA PDF in `folder`, using the saved schedule.csv when it still
    matches the PDFs. -> (digest, flights) or (None, []) when the folder has no PDFs."""
    pdfs = [os.path.join(folder, f) for f in sorted(os.listdir(folder))
            if f.lower().endswith(".pdf")] if os.path.isdir(folder) else []
    cache = os.path.join(folder, CSV_NAME)
    if not pdfs:
        if os.path.exists(cache):  # the CSV alone is enough (e.g. PDFs not committed)
            return read_csv(cache)
        return None, []
    digest = pdf_digest(pdfs)
    if os.path.exists(cache):
        cached_digest, rows = read_csv(cache)
        if cached_digest == digest:
            return digest, rows
    parsed = []
    for p in pdfs:
        log(f"Reading {os.path.basename(p)} …")
        parsed.append(parse_pdf(p))
    flights = build_flights(parsed)
    try:
        write_csv(cache, digest, flights)
    except OSError:
        pass  # read-only folder: still usable, just re-read next time
    return digest, flights


def import_flights(conn, flights, digest, source_name="DGCA schedule"):
    """Replace the DGCA flights in the database with `flights` (rows as in schedule.csv)."""
    rows = [(f["Origin"], f["Destination"], f["Airline"], f["FlightNo"], f["DepTime"],
             *[int(f[d] or 0) for d in DAYS], f["EffectiveFrom"] or None, f["EffectiveTo"] or None,
             f.get("Aircraft") or None) for f in flights]
    conn.execute("DELETE FROM Flights WHERE Source = 'dgca'")
    conn.executemany(
        "INSERT INTO Flights (Origin, Destination, Airline, FlightNo, DepTime, Mon, Tue, Wed, Thu, Fri, Sat, Sun, "
        "EffectiveFrom, EffectiveTo, Aircraft, Source) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'dgca')", rows)

    # Airports the workbook doesn't list: add them with the city name from the PDFs (not as origins).
    for f in flights:
        for code, name in ((f["Origin"], f.get("OriginName")), (f["Destination"], f.get("DestName"))):
            conn.execute("INSERT OR IGNORE INTO Stations (Code, Name, IsHub, SortOrder) VALUES (?, ?, 0, 999)",
                         (code, name or None))
            if name:
                conn.execute("UPDATE Stations SET Name = ? WHERE Code = ? AND (Name IS NULL OR Name = '')",
                             (name, code))
    # Airlines that only appear in the PDFs (no tariff loaded for them yet)
    for prefix, (code, name) in AIRLINES.items():
        if any(f["Airline"] == code for f in flights):
            conn.execute("INSERT OR IGNORE INTO Airlines (Code, Name) VALUES (?, ?)", (code, name))

    seasons = sorted({f.get("Season") or "" for f in flights} - {""})
    dates_from = [f["EffectiveFrom"] for f in flights if f["EffectiveFrom"]]
    dates_to = [f["EffectiveTo"] for f in flights if f["EffectiveTo"]]
    state = {"dgca_sha256": digest, "dgca_season": "/".join(seasons),
             "dgca_valid_from": min(dates_from, default=""), "dgca_valid_to": max(dates_to, default=""),
             "dgca_source": source_name}
    for k, v in state.items():
        conn.execute("INSERT INTO AppState (Key, Value) VALUES (?, ?) "
                     "ON CONFLICT(Key) DO UPDATE SET Value = excluded.Value", (k, v))
    conn.commit()
    return len(rows)


def folder_digest(folder):
    """Digest of the PDFs in `folder` (or of the saved CSV when there are no PDFs) - fast, no parsing."""
    if not os.path.isdir(folder):
        return None
    pdfs = [os.path.join(folder, f) for f in os.listdir(folder) if f.lower().endswith(".pdf")]
    if pdfs:
        return pdf_digest(pdfs)
    cache = os.path.join(folder, CSV_NAME)
    if os.path.exists(cache):
        with open(cache, encoding="utf-8") as f:
            first = f.readline()
        return first.split(":", 1)[1].strip() if first.startswith("# pdf-sha256:") else "csv-only"
    return None
