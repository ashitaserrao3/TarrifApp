"""
Flight -> tariff matching engine (replaces the old link_rates.py).

For every scheduled flight it finds the right general-cargo rate row in the
rate file that was in force on the chosen date, works out the per-kg
surcharges that apply, and adds the airline's handling add-on to give a
cost per kg (CPKG).

Rate rows carry a free-text condition (IndiGo "Applicable Flights" column,
Air India "Applicable Flight Number" column). The wording drifts a lot, so
it's parsed into one of:

    all       - no condition, the route rate for every flight
    flights   - only the listed flight numbers (minus any "excluding" ones)
    except    - every flight except the listed ones
    window    - flights departing inside a time window (may wrap midnight)
    unknown   - text we couldn't read -> never matched, flagged for review

The most specific matching row wins: flights > except/window > all.
"""

import datetime as dt
import re
from dataclasses import dataclass, field

# Which products are general cargo (what the dashboard prices). Special
# cargo (SCR, HEA, DGR, XPS...) is browsable on the Tariffs page.
GENERAL_PRODUCTS = {"IndiGo": ["GCR", "LEAN"], "Air India": ["GCR", "PRIME"], "Akasa": ["GCR", "PRIME"],
                    "SpiceJet": ["GCR", "PRIME", "LEAN"], "Star Air": ["GCR"]}
CONDITION_COL = {"IndiGo": "ApplicableFlights", "Air India": "ApplicableFlightNumber", "Akasa": "ApplicableFlights",
                 "SpiceJet": "ApplicableFlights", "Star Air": "ApplicableFlights"}
TABLE_PREFIX = {"IndiGo": "IndiGo", "Air India": "AirIndia", "Akasa": "Akasa", "SpiceJet": "SpiceJet",
                "Star Air": "StarAir"}
# When rows of different products match a flight equally (e.g. SpiceJet's "PRIME - All Flights" next to
# the route's GCR rate), the more specific product wins.
PRODUCT_PRIORITY = {"PRIME": 0, "LEAN": 1, "GCR": 2}

BREAKS = [(0, "RateN"), (45, "RateQ45"), (100, "RateQ100"), (250, "RateQ250"), (300, "RateQ300"),
          (500, "RateQ500"), (1000, "RateQ1000")]

STATUS_OK = "OK"
STATUS_NO_TARIFF = "NO TARIFF LOADED"
STATUS_NO_RATE = "NO RATE"
STATUS_CHECK = "CHECK"

# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

FLIGHT_RE = re.compile(r"(?<![A-Z0-9])(6E|AI|IX|QP|SG|UK|I5|9I)\s*-?\s*(\d{1,4})([A-Z]?)(?![0-9])", re.I)
TIME_RE = re.compile(
    r"(\d{1,2})[:.](\d{2})\s*(?:hrs?\.?|hours)?\s*(?:to|-|–|till|until)\s*(\d{1,2})[:.](\d{2})", re.I)
EXCEPT_RE = re.compile(r"\b(except|excluding|exclude)\b", re.I)
AMOUNT_RE = re.compile(r"(\d+(?:\.\d+)?)")


def norm_flight(text):
    """'AI-0441' / 'AI 441' / 'ai441' -> 'AI441'.  None if no flight number."""
    if not text:
        return None
    m = FLIGHT_RE.search(str(text))
    if not m:
        return None
    return f"{m.group(1).upper()}{int(m.group(2))}{m.group(3).upper()}"


def parse_dep_minutes(t):
    """'17:55' -> 1075. None when the schedule time isn't a real time."""
    if t is None:
        return None
    m = re.fullmatch(r"\s*(\d{1,2})[:.](\d{2})\s*", str(t))
    if not m:
        return None
    h, mi = int(m.group(1)), int(m.group(2))
    if h > 24 or mi > 59:
        return None
    return (h * 60 + mi) % 1440


@dataclass
class Condition:
    kind: str
    text: str = ""
    flights: frozenset = frozenset()
    excluded: frozenset = frozenset()
    window: tuple = None  # (start_min, end_min) inclusive, may wrap

    RANK = {"flights": 3, "except": 2, "window": 2, "all": 1, "unknown": 0}

    @property
    def rank(self):
        return self.RANK[self.kind]

    def in_window(self, minutes):
        start, end = self.window
        if start <= end:
            return start <= minutes <= end
        return minutes >= start or minutes <= end

    def matches(self, flight_no, dep_minutes):
        """True / False, or None when it can't be decided (bad dep time)."""
        if self.kind == "all":
            return True
        if self.kind == "flights":
            return flight_no in self.flights and flight_no not in self.excluded
        if self.kind == "except":
            return flight_no not in self.excluded
        if self.kind == "window":
            if dep_minutes is None:
                return None
            return self.in_window(dep_minutes)
        return False

    @property
    def note(self):
        return {"all": "Route rate (all flights)",
                "flights": "Flight-specific rate",
                "except": "All flights except listed ones",
                "window": "Departure time within window",
                "unknown": "Condition text not understood"}[self.kind]


def _window(m):
    sh, sm, eh, em = (int(x) for x in m.groups())
    start = sh * 60 + sm
    end = eh * 60 + em
    if start == 1:          # "00:01" really means "from midnight"
        start = 0
    if end == 0 or end >= 1439:   # "00:00", "23:59", "24:00" -> end of day
        end = 1440
    start %= 1440
    return (start, end)


_cond_cache = {}


def parse_condition(text) -> Condition:
    if text is None or not str(text).strip():
        return Condition("all")
    t = " ".join(str(text).split())
    if t in _cond_cache:
        return _cond_cache[t]
    m = EXCEPT_RE.search(t)
    inc_part, exc_part = (t[:m.start()], t[m.start():]) if m else (t, "")

    def flights(part):
        return frozenset(f"{a.upper()}{int(b)}{c.upper()}" for a, b, c in FLIGHT_RE.findall(part))

    inc, exc = flights(inc_part), flights(exc_part)
    win = TIME_RE.search(t)
    if inc:
        c = Condition("flights", t, inc, exc)
    elif exc:
        c = Condition("except", t, excluded=exc)
    elif win:
        c = Condition("window", t, window=_window(win))
    elif re.search(r"\ball\s+flights?\b", t, re.I):
        c = Condition("all", t)
    else:
        c = Condition("unknown", t)
    _cond_cache[t] = c
    return c


def amount(text):
    if text is None:
        return None
    if isinstance(text, (int, float)):
        return float(text)
    m = AMOUNT_RE.search(str(text))
    return float(m.group(1)) if m else None


_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
_END_RE = re.compile(r"(?:to|till|until|upto|up to)\s+(\d{1,2})(?:st|nd|rd|th)?[-\s]?([A-Za-z]{3,9})[-\s]?(\d{2,4})?",
                     re.I)
_YEAR_RE = re.compile(r"\b(20\d{2})\b|-(\d{2})\b")


def remark_end_date(remark, default_year):
    """'... Effective 16-Jul to 31-Aug 2026' -> date(2026, 8, 31)."""
    if not remark:
        return None
    m = _END_RE.search(remark)
    if not m:
        return None
    mon = _MONTHS.get(m.group(2)[:3].lower())
    if not mon:
        return None
    year = m.group(3)
    if year is None:
        y = _YEAR_RE.search(remark[m.end():]) or _YEAR_RE.search(remark)
        year = (y.group(1) or y.group(2)) if y else str(default_year)
    year = int(year)
    if year < 100:
        year += 2000
    try:
        return dt.date(year, mon, int(m.group(1)))
    except ValueError:
        return None


_COMMODITY_RE = re.compile(r"\(([A-Z]{3}(?:\s*,\s*[A-Z]{3})*)\)")


def surcharge_applies(text, flight_no):
    """Surcharge cells sometimes narrow themselves down, e.g.
    'INR 5/- per kg (PEF) on 6E975, 6E952' - only for that commodity (PEF)
    on those flights. Those don't apply to general cargo."""
    s = str(text)
    if _COMMODITY_RE.search(s):
        return False
    listed = {f"{a.upper()}{int(b)}{c.upper()}" for a, b, c in FLIGHT_RE.findall(s)}
    return not listed or flight_no in listed


def to_date(v):
    if v is None or v == "":
        return None
    if isinstance(v, dt.datetime):
        return v.date()
    if isinstance(v, dt.date):
        return v
    return dt.date.fromisoformat(str(v)[:10])


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------

@dataclass
class Surcharge:
    label: str
    amount: float
    reason: str
    flag: str = ""      # e.g. "ended 31-Aug-26 - confirm"

    @property
    def short(self):
        s = f"{self.label} ₹{self.amount:g}"
        return f"{s} ({self.flag})" if self.flag else s


@dataclass
class FlightRate:
    flight: dict
    status: str
    product: str = None
    effective_date: dt.date = None
    source_file: str = None
    rate_row: dict = None
    condition: Condition = None
    match_note: str = ""
    surcharges: list = field(default_factory=list)
    addon: float = None
    addon_note: str = None
    notes: list = field(default_factory=list)

    # -- convenience -----------------------------------------------------
    @property
    def rates(self):
        r = self.rate_row or {}
        return {"Min": r.get("MinCharge"), "N": r.get("RateN"), "45": r.get("RateQ45"),
                "100": r.get("RateQ100"), "300": r.get("RateQ300"), "500": r.get("RateQ500"),
                "1000": r.get("RateQ1000")}

    @property
    def surcharge_total(self):
        return sum(s.amount for s in self.surcharges) if self.rate_row else None

    @property
    def surcharge_summary(self):
        return " + ".join(s.short for s in self.surcharges)

    def rate_at(self, weight):
        """Per-kg rate for the weight break that applies at `weight`, falling
        back to the next lower break that the tariff actually defines.
        Returns (rate, break_label_used)."""
        if not self.rate_row:
            return None, None
        best = (None, None)
        for brk, col in BREAKS:
            v = self.rate_row.get(col)
            if v is not None and brk <= weight:
                best = (v, "N" if brk == 0 else f"{brk}+")
        return best

    def cpkg(self, weight):
        """Tariff rate + surcharges + add-on at a weight break (₹/kg)."""
        rate, used = self.rate_at(weight)
        if rate is None:
            return None, used
        return rate + (self.surcharge_total or 0) + (self.addon or 0), used

    def freight_charge(self, weight):
        """Air-freight charge for a chargeable weight, applying the 'lower
        charge at a higher weight break' rule and the minimum charge."""
        if not self.rate_row or not weight or weight <= 0:
            return None
        options = []
        for brk, col in BREAKS:
            v = self.rate_row.get(col)
            if v is not None:
                options.append(v * max(weight, brk))
        if not options:
            return None
        charge = min(options)
        mc = self.rate_row.get("MinCharge")
        if mc is not None:
            charge = max(charge, mc)
        return charge

    def total_cost(self, weight):
        fc = self.freight_charge(weight)
        if fc is None:
            return None
        return fc + ((self.surcharge_total or 0) + (self.addon or 0)) * weight

    @property
    def key(self):
        f = self.flight
        return f"{f['Origin']}|{f['Destination']}|{f['FlightNo']}|{f['DepTime']}"


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

class TariffEngine:
    """Price flights as of a given date (default: today)."""

    def __init__(self, conn, as_of: dt.date = None):
        self.conn = conn
        self.as_of = as_of or dt.date.today()
        self.airlines = {r["Code"]: dict(r) for r in conn.execute("SELECT * FROM Airlines")}
        self._batches = {}
        self._rows = {}       # (carrier, origin, dest) -> [row dicts]
        self._loaded = set()  # (carrier, origin) already pulled from DB
        self._network = [dict(r) for r in conn.execute(
            "SELECT * FROM NetworkSurcharges WHERE Active = 1 ORDER BY SurchargeID")]
        self._batch_surcharges = {}
        self._tariff_code = {r["Code"]: r["TariffCode"] for r in conn.execute(
            "SELECT Code, TariffCode FROM Stations WHERE TariffCode IS NOT NULL AND TariffCode <> ''")}

    def tariff_code(self, station):
        return self._tariff_code.get(station, station)

    # -- batches ---------------------------------------------------------
    def batch(self, carrier):
        """The rate file in force on as_of (latest effective date <= as_of)."""
        if carrier not in self._batches:
            r = self.conn.execute(
                "SELECT * FROM LoadBatches WHERE Carrier = ? AND Status = 'OK' "
                "AND (EffectiveDate IS NULL OR substr(EffectiveDate, 1, 10) <= ?) "
                "ORDER BY EffectiveDate DESC, BatchID DESC LIMIT 1",
                (carrier, self.as_of.isoformat())).fetchone()
            self._batches[carrier] = dict(r) if r else None
        return self._batches[carrier]

    def batches(self, carrier):
        """Every rate file of the carrier in force on as_of, newest first."""
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM LoadBatches WHERE Carrier = ? AND Status = 'OK' "
            "AND (EffectiveDate IS NULL OR substr(EffectiveDate, 1, 10) <= ?) "
            "ORDER BY EffectiveDate DESC, BatchID DESC", (carrier, self.as_of.isoformat()))]

    def _lane_rows(self, carrier, origin, dest):
        """Rate rows for a lane. The newest file in force wins for every line it has; an older file only
        fills in the lines (product + flight condition) the newer files don't have, so a file holding just
        the changed lines updates those lines and leaves the rest as they were."""
        if (carrier, origin) not in self._loaded:
            seen = set()
            for b in self.batches(carrier):
                lines = {}
                for product in GENERAL_PRODUCTS[carrier]:
                    table = f"{TABLE_PREFIX[carrier]}_{product}"
                    for r in self.conn.execute(
                            f"SELECT * FROM {table} WHERE BatchID = ? AND OriginCode = ? "
                            f"AND (ApplicableCommodity IS NULL OR TRIM(ApplicableCommodity) = '') "
                            f"ORDER BY RateID", (b["BatchID"], origin)):
                        d = dict(r)
                        line = (d["DestCode"], product, re.sub(r"\s+", " ", str(d.get(CONDITION_COL[carrier]) or ""))
                                .strip().upper())
                        if line in seen:
                            continue
                        d["_Product"] = product
                        d["_Condition"] = parse_condition(d.get(CONDITION_COL[carrier]))
                        lines[line] = True
                        self._rows.setdefault((carrier, origin, d["DestCode"]), []).append(d)
                seen |= lines.keys()
            self._loaded.add((carrier, origin))
        return self._rows.get((carrier, origin, dest), [])

    # -- pricing ---------------------------------------------------------
    def price(self, flight) -> FlightRate:
        flight = dict(flight)
        airline = self.airlines.get(flight["Airline"], {})
        carrier = airline.get("TariffCarrier")
        res = FlightRate(flight=flight, status=STATUS_NO_TARIFF)
        res.addon = airline.get("AddOnPerKg")
        res.addon_note = airline.get("AddOnIncludes")
        if not carrier or not self.batch(carrier):
            res.match_note = ("This airline's tariff is not loaded in the database" if not carrier
                              else f"No {carrier} rate file effective on or before {self.as_of:%d-%b-%y}")
            return res

        b = self.batch(carrier)
        res.effective_date = to_date(b["EffectiveDate"])
        res.source_file = b["SourceFileName"]
        rows = self._lane_rows(carrier, self.tariff_code(flight["Origin"]),
                               self.tariff_code(flight["Destination"]))
        if not rows:
            res.status = STATUS_NO_RATE
            res.match_note = f"No general-cargo rate for {flight['Origin']}-{flight['Destination']} in this file"
            return res

        fno = norm_flight(flight["FlightNo"])
        dep = parse_dep_minutes(flight.get("DepTime"))
        matched, undecided = [], []
        for r in rows:
            ok = r["_Condition"].matches(fno, dep)
            if ok:
                matched.append(r)
            elif ok is None:
                undecided.append(r)

        best_rank = max((r["_Condition"].rank for r in matched), default=0)
        if undecided and best_rank < 3:
            # A departure-time rule exists but the schedule time is unusable.
            chosen = undecided[0]
            res.status = STATUS_CHECK
            res.match_note = (f"Departure time '{flight.get('DepTime')}' in schedule is not a valid time - "
                              f"can't pick the time-based rate")
        elif matched:
            top = [r for r in matched if r["_Condition"].rank == best_rank]
            best_product = min(PRODUCT_PRIORITY.get(r["_Product"], 9) for r in top)
            top = [r for r in top if PRODUCT_PRIORITY.get(r["_Product"], 9) == best_product]
            chosen = top[0]
            res.status = STATUS_OK
            res.match_note = chosen["_Condition"].note
            if len({(r["_Product"], r.get("RateN"), r.get("RateQ45"), r.get("RateQ100")) for r in top}) > 1:
                res.status = STATUS_CHECK
                res.match_note += f" - {len(top)} rate rows match equally, showing the first"
        else:
            res.status = STATUS_NO_RATE
            unknown = [r for r in rows if r["_Condition"].kind == "unknown"]
            if unknown:
                res.status = STATUS_CHECK
                res.match_note = "Rate condition text not understood - check manually"
                res.condition = unknown[0]["_Condition"]
            else:
                res.match_note = "No rate row on this route applies to this flight / departure time"
            return res

        res.rate_row = chosen
        res.product = chosen["_Product"]
        res.condition = chosen["_Condition"]
        res.surcharges = self._surcharges(carrier, flight, chosen)
        return res

    def _surcharges(self, carrier, flight, row):
        out = []
        cond = row["_Condition"]
        remark = row.get("Remarks")
        end = remark_end_date(remark, self.as_of.year)
        flag = f"ended {end:%d-%b-%y} - confirm" if end and end < self.as_of else ""
        commodity_only = bool(remark and re.search(r"\bonly\s+for\b", remark, re.I))

        fno = norm_flight(flight["FlightNo"])

        def add(label, text, reason):
            amt = amount(text)
            if not amt:
                return
            if not surcharge_applies(text, fno):
                return
            full = f"{carrier} {label.lower()} surcharge"
            if remark:
                full += f" - {remark.lstrip('- ')}"
            if commodity_only:
                # e.g. "commercial surcharge ... (Only for FRUITS, VEG, PER, CHR)" - not general cargo
                return
            out.append(Surcharge(label, amt, reason or full, flag))

        if carrier != "Air India":
            add("Sector", row.get("SectorSurcharge"), None)
            add("Flight", row.get("FlightSurcharge"), None)
        else:
            add("Flight" if cond.kind == "flights" else "Sector", row.get("Surcharge"), None)
            add("Seasonal", row.get("SeasonalSurcharge"), None)

        o, d = flight["Origin"], flight["Destination"]
        out += self._file_surcharges(carrier, self.tariff_code(o), self.tariff_code(d), fno)
        for s in self._network:
            if s["Carrier"] != carrier:
                continue
            vf, vt = to_date(s["ValidFrom"]), to_date(s["ValidTo"])
            if (vf and vf > self.as_of) or (vt and vt < self.as_of):
                continue
            applies, st = (s["AppliesTo"] or "").lower(), (s["Station"] or "").upper()
            if (applies == "all"
                    or (applies == "origin" and st == o)
                    or (applies == "destination" and st == d)
                    or (applies == "route" and st == f"{o}-{d}")):
                out.append(Surcharge(s["Label"] or s["AppliesTo"], s["RatePerKg"], s["Reason"] or ""))
        return out

    def _file_surcharges(self, carrier, o, d, fno):
        """Surcharges stated in the carrier's tariff file in force (BatchSurcharges). A surcharge
        for this exact route replaces a network-wide one into the same destination."""
        b = self.batch(carrier)
        if not b:
            return []
        if carrier not in self._batch_surcharges:
            self._batch_surcharges[carrier] = [dict(r) for r in self.conn.execute(
                "SELECT * FROM BatchSurcharges WHERE BatchID = ? ORDER BY SurchargeID", (b["BatchID"],))]

        def listed(v):
            return {x.strip().upper() for x in (v or "").split(",") if x.strip()}

        hits = []
        for s in self._batch_surcharges[carrier]:
            vf, vt = to_date(s["ValidFrom"]), to_date(s["ValidTo"])
            if (vf and vf > self.as_of) or (vt and vt < self.as_of):
                continue
            origins, dests = listed(s["Origins"]), listed(s["Destinations"])
            if (origins and o not in origins) or (dests and d not in dests) or o in listed(s["ExceptOrigins"]):
                continue
            flights = {norm_flight(f) for f in listed(s["Flights"])}
            if (flights and fno not in flights) or fno in {norm_flight(f) for f in listed(s["ExceptFlights"])}:
                continue
            hits.append(s)
        specific_dests = {d for s in hits if s["Origins"] and s["Destinations"]}
        hits = [s for s in hits if s["Origins"] or not s["Destinations"] or d not in specific_dests]
        return [Surcharge(s["Label"] or "Surcharge", s["RatePerKg"], s["Reason"] or "") for s in hits]

    def price_all(self, flights):
        return [self.price(f) for f in flights]


def flights_for_route(conn, origin, dest):
    return [dict(r) for r in conn.execute(
        "SELECT * FROM Flights WHERE Origin = ? AND Destination = ? ORDER BY DepTime, FlightNo",
        (origin, dest))]
