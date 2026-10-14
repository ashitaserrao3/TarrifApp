"""
Database schema (SQLite).

The rate tables keep exactly the same layout as the original CargoRates
Access database - one table per product, per carrier, every row tagged with
its BatchID + EffectiveDate so history is never overwritten. On top of that
the web app adds tables for the flight schedule, airlines/add-ons and the
network-wide surcharges that used to live in the Excel workbook.
"""

AIRINDIA_TABLES = ["GCR", "PRIME", "SCR", "HEA", "Other"]
INDIGO_TABLES = ["GCR", "LEAN", "SCR", "HEA", "XPS", "DGR", "RFL", "RLI", "Other"]

AKASA_TABLES = ["GCR", "PRIME", "Other"]
SPICEJET_TABLES = ["GCR", "PRIME", "LEAN", "Other"]
STARAIR_TABLES = ["GCR", "Other"]

CARRIER_PREFIX = {"Air India": "AirIndia", "IndiGo": "IndiGo", "Akasa": "Akasa", "SpiceJet": "SpiceJet",
                  "Star Air": "StarAir"}
CARRIER_TABLES = {"Air India": AIRINDIA_TABLES, "IndiGo": INDIGO_TABLES, "Akasa": AKASA_TABLES,
                  "SpiceJet": SPICEJET_TABLES, "Star Air": STARAIR_TABLES}
# Carriers whose files are read by the generic reader (parsers.read_akasa / read_spicejet / read_starair)
GENERIC_CARRIERS = {"Akasa": AKASA_TABLES, "SpiceJet": SPICEJET_TABLES, "Star Air": STARAIR_TABLES}
# Airline code in the schedule -> tariff carrier
AIRLINE_CARRIER = {"INDIGO": "IndiGo", "AIRINDIA": "Air India", "AKASA": "Akasa", "SPICEJET": "SpiceJet",
                   "STARAIR": "Star Air"}

_AIRINDIA_RATE_COLS = """
    RateID                  INTEGER PRIMARY KEY AUTOINCREMENT,
    BatchID                 INTEGER NOT NULL REFERENCES LoadBatches(BatchID) ON DELETE CASCADE,
    EffectiveDate           TEXT,
    Region                  TEXT,
    OriginCode              TEXT,
    DestCode                TEXT,
    Sector                  TEXT,
    ApplicableFlightNumber  TEXT,
    RateCategory            TEXT,
    ApplicableCommodity     TEXT,
    MinCharge               REAL,
    RateN                   REAL,
    RateQ45                 REAL,
    RateQ100                REAL,
    RateQ300                REAL,
    RateQ500                REAL,
    RateQ1000               REAL,
    Surcharge               TEXT,
    SeasonalSurcharge       TEXT,
    Remarks                 TEXT,
    SourceSheet             TEXT
"""

_INDIGO_RATE_COLS = """
    RateID               INTEGER PRIMARY KEY AUTOINCREMENT,
    BatchID              INTEGER NOT NULL REFERENCES LoadBatches(BatchID) ON DELETE CASCADE,
    EffectiveDate        TEXT,
    Region               TEXT,
    OriginCode           TEXT,
    DestCode             TEXT,
    RateCategory         TEXT,
    MinCharge            REAL,
    RateN                REAL,
    RateQ45              REAL,
    RateQ100             REAL,
    RateQ300             REAL,
    RateQ500             REAL,
    RateQ1000            REAL,
    ApplicableFlights    TEXT,
    ApplicableCommodity  TEXT,
    SectorSurcharge      TEXT,
    FlightSurcharge      TEXT,
    CommoditySurcharge   TEXT,
    SourceSheet          TEXT
"""


_GENERIC_RATE_COLS = """
    RateID               INTEGER PRIMARY KEY AUTOINCREMENT,
    BatchID              INTEGER NOT NULL REFERENCES LoadBatches(BatchID) ON DELETE CASCADE,
    EffectiveDate        TEXT,
    Region               TEXT,
    OriginCode           TEXT,
    DestCode             TEXT,
    RateCategory         TEXT,
    MinCharge            REAL,
    RateN                REAL,
    RateQ45              REAL,
    RateQ100             REAL,
    RateQ250             REAL,
    RateQ300             REAL,
    RateQ500             REAL,
    RateQ1000            REAL,
    ApplicableFlights    TEXT,
    ApplicableCommodity  TEXT,
    SectorSurcharge      TEXT,
    FlightSurcharge      TEXT,
    CommoditySurcharge   TEXT,
    Remarks              TEXT,
    SourceSheet          TEXT
"""


def rate_table_names():
    names = []
    for carrier, suffixes in CARRIER_TABLES.items():
        names += [f"{CARRIER_PREFIX[carrier]}_{s}" for s in suffixes]
    return names


def build_schema_sql() -> str:
    parts = ["""
CREATE TABLE IF NOT EXISTS LoadBatches (
    BatchID          INTEGER PRIMARY KEY AUTOINCREMENT,
    Carrier          TEXT NOT NULL,
    SourceFileName   TEXT NOT NULL,
    EffectiveDate    TEXT,
    LoadedAt         TEXT,
    RowsLoaded       INTEGER,
    Status           TEXT,
    Notes            TEXT
);
"""]
    for suffix in AIRINDIA_TABLES:
        t = f"AirIndia_{suffix}"
        parts.append(f"CREATE TABLE IF NOT EXISTS {t} ({_AIRINDIA_RATE_COLS});")
        parts.append(f"CREATE INDEX IF NOT EXISTS ix_{t}_lane ON {t}(BatchID, OriginCode, DestCode);")
        parts.append(f"CREATE INDEX IF NOT EXISTS ix_{t}_od ON {t}(OriginCode, DestCode);")
    for suffix in INDIGO_TABLES:
        t = f"IndiGo_{suffix}"
        parts.append(f"CREATE TABLE IF NOT EXISTS {t} ({_INDIGO_RATE_COLS});")
        parts.append(f"CREATE INDEX IF NOT EXISTS ix_{t}_lane ON {t}(BatchID, OriginCode, DestCode);")
        parts.append(f"CREATE INDEX IF NOT EXISTS ix_{t}_od ON {t}(OriginCode, DestCode);")

    for carrier, suffixes in GENERIC_CARRIERS.items():
        for suffix in suffixes:
            t = f"{CARRIER_PREFIX[carrier]}_{suffix}"
            parts.append(f"CREATE TABLE IF NOT EXISTS {t} ({_GENERIC_RATE_COLS});")
            parts.append(f"CREATE INDEX IF NOT EXISTS ix_{t}_lane ON {t}(BatchID, OriginCode, DestCode);")
            parts.append(f"CREATE INDEX IF NOT EXISTS ix_{t}_od ON {t}(OriginCode, DestCode);")

    parts.append("""
-- Surcharges stated in a carrier's tariff file (fuel, security, route and flight surcharges),
-- kept with the file they came from so they change when a new file is loaded.
-- Comma lists; empty = any. ExceptOrigins / ExceptFlights exclude.
CREATE TABLE IF NOT EXISTS BatchSurcharges (
    SurchargeID    INTEGER PRIMARY KEY AUTOINCREMENT,
    BatchID        INTEGER NOT NULL REFERENCES LoadBatches(BatchID) ON DELETE CASCADE,
    Origins        TEXT,
    Destinations   TEXT,
    ExceptOrigins  TEXT,
    Flights        TEXT,
    ExceptFlights  TEXT,
    RatePerKg      REAL NOT NULL,
    Label          TEXT,
    Reason         TEXT,
    ValidFrom      TEXT,
    ValidTo        TEXT
);

CREATE TABLE IF NOT EXISTS AirIndia_CommodityCodes (
    CommodityCodeID  INTEGER PRIMARY KEY AUTOINCREMENT,
    BatchID          INTEGER REFERENCES LoadBatches(BatchID) ON DELETE CASCADE,
    LMSCode          TEXT,
    Description      TEXT,
    iCargoCode       TEXT
);

CREATE TABLE IF NOT EXISTS AirIndia_Notes (
    NoteID        INTEGER PRIMARY KEY AUTOINCREMENT,
    BatchID       INTEGER REFERENCES LoadBatches(BatchID) ON DELETE CASCADE,
    EffectiveDate TEXT,
    SectionNo     TEXT,
    HeadingText   TEXT,
    DetailText    TEXT,
    RowOrder      INTEGER
);

CREATE TABLE IF NOT EXISTS IndiGo_Footnotes (
    FootnoteID     INTEGER PRIMARY KEY AUTOINCREMENT,
    BatchID        INTEGER REFERENCES LoadBatches(BatchID) ON DELETE CASCADE,
    EffectiveDate  TEXT,
    SeqNo          TEXT,
    FootnoteText   TEXT
);

CREATE TABLE IF NOT EXISTS IndiGo_TSPCharges (
    TSPID               INTEGER PRIMARY KEY AUTOINCREMENT,
    BatchID             INTEGER REFERENCES LoadBatches(BatchID) ON DELETE CASCADE,
    EffectiveDate       TEXT,
    Station             TEXT,
    TariffCategory      TEXT,
    OutboundRatePerKg   REAL,
    OutboundMinCharge   REAL,
    InboundRatePerKg    REAL,
    InboundMinCharge    REAL,
    Remarks             TEXT
);

-- ---------------------------------------------------------------------------
-- Web-app tables (these used to be tabs in the flight-schedule workbook)
-- ---------------------------------------------------------------------------

-- Airport / station codes. IsHub = 1 for the cities you ship FROM (the
-- Origin drop-down); every station appears in the Destination drop-down.
CREATE TABLE IF NOT EXISTS Stations (
    Code       TEXT PRIMARY KEY,
    Name       TEXT,
    IsHub      INTEGER NOT NULL DEFAULT 0,
    SortOrder  INTEGER,
    TariffCode TEXT     -- code the airlines' rate files use, if different (TVM -> TRV)
);

-- Airlines as they appear in the schedule. TariffCarrier links an airline
-- to the rate tables ('IndiGo' / 'Air India'); NULL = no tariff loaded yet.
CREATE TABLE IF NOT EXISTS Airlines (
    Code           TEXT PRIMARY KEY,
    Name           TEXT,
    TariffCarrier  TEXT,
    AddOnPerKg     REAL,
    AddOnIncludes  TEXT
);

-- Nonstop flight schedule (one row per flight number + departure time).
CREATE TABLE IF NOT EXISTS Flights (
    FlightID     INTEGER PRIMARY KEY AUTOINCREMENT,
    Origin       TEXT NOT NULL,
    Destination  TEXT NOT NULL,
    Airline      TEXT NOT NULL,
    FlightNo     TEXT NOT NULL,
    DepTime      TEXT,
    Mon INTEGER DEFAULT 0, Tue INTEGER DEFAULT 0, Wed INTEGER DEFAULT 0,
    Thu INTEGER DEFAULT 0, Fri INTEGER DEFAULT 0, Sat INTEGER DEFAULT 0,
    Sun INTEGER DEFAULT 0,
    Source         TEXT,   -- 'workbook' (Raw Data sheet) or 'dgca' (DGCA schedule PDFs)
    EffectiveFrom  TEXT,   -- YYYY-MM-DD; DGCA rows are only valid between these dates
    EffectiveTo    TEXT,
    Aircraft       TEXT
);
CREATE INDEX IF NOT EXISTS ix_Flights_od ON Flights(Origin, Destination);

-- Small key/value store (e.g. which schedule workbook from the data folder is loaded).
CREATE TABLE IF NOT EXISTS AppState (
    Key    TEXT PRIMARY KEY,
    Value  TEXT
);

CREATE TABLE IF NOT EXISTS ScheduleImports (
    ImportID        INTEGER PRIMARY KEY AUTOINCREMENT,
    SourceFileName  TEXT,
    ImportedAt      TEXT,
    FlightCount     INTEGER,
    Notes           TEXT
);

-- Surcharges from the tariff notes / footnotes that apply across many
-- routes (fuel, SS, ex-BOM, festive...). Route- and flight-specific
-- surcharges are read from the rate tables automatically.
CREATE TABLE IF NOT EXISTS NetworkSurcharges (
    SurchargeID  INTEGER PRIMARY KEY AUTOINCREMENT,
    Carrier      TEXT NOT NULL,          -- 'IndiGo' | 'Air India'
    AppliesTo    TEXT NOT NULL,          -- All | Origin | Destination | Route
    Station      TEXT,                   -- BOM, or DEL-COK for Route
    RatePerKg    REAL NOT NULL,
    ValidFrom    TEXT,                   -- YYYY-MM-DD, blank = always
    ValidTo      TEXT,                   -- YYYY-MM-DD, blank = until further notice
    Reason       TEXT,
    Active       INTEGER NOT NULL DEFAULT 1,
    Label        TEXT
);
""")
    return "\n".join(parts)
