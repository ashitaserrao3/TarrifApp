# Cargo Tariffs — air cargo rate lookup web app

A small web app that replaces the *CargoRates* Access database plus the
*ATC/AOC Flight Schedule* Excel workbook. You upload the Air India and IndiGo
domestic rate lists as they arrive (every ~15 days). The app keeps every
cycle, links every scheduled flight to the rate that applies to it, and shows
the **cost per kg (CPKG)** for any route:

> CPKG = tariff rate + surcharges + your add-on (X-ray / TSP / handling)

## What's in it

| Page | What it does |
|---|---|
| **Route lookup** | Pick origin, destination, the date the rates should apply on, and optionally a shipment weight. Shows every nonstop flight with its airline, departure time, days of operation, product (GCR / LEAN / PRIME), rate, surcharges, add-on and CPKG at 45+ and 100+ kg. With a weight, it also shows the total cost, using the minimum charge and the "cheaper at a higher weight break" rule. The cheapest option is highlighted. *(This is the old Dashboard sheet.)* |
| **All flights** | Every flight with its linked rate, filters, and **CSV download**. *(This is the old Flight Rates sheet.)* |
| **Rate history** | How a lane's rate moved across every file you've loaded, as a chart and a table. |
| **Tariffs** | Browse any loaded rate file, any product (GCR, LEAN, PRIME, SCR, HEA, XPS, DGR…). |
| **Notes** | Air India Notation, IndiGo footnotes and TSP charges from the latest files. |
| **Exceptions** | Flights the app couldn't price confidently, surcharges whose validity has ended, airlines with no tariff, and rows the loader couldn't classify. |
| **Upload** | Upload rate files and the schedule workbook. Shows every file loaded, and lets you delete one. |
| **Settings** | Network-wide surcharges (fuel, SS, ex-BOM, festive…), airline add-on costs, and stations (which are hubs, plus code aliases such as TVM → TRV). |

### How a flight is matched to its rate

The flight-to-rate matching rules were rebuilt from the workbook's
*Flight Rates* sheet. Against the same rate files, the app reproduces that
sheet exactly: all 1,630 flights match on product, rate and surcharges.

1. **Which rate file:** the latest file for that carrier whose effective
   date is on or before the *rates as of* date. Change the date to see what
   a route cost in the past.
2. **Which rate row:** only general cargo is considered (IndiGo GCR / LEAN,
   Air India GCR / PRIME). The condition text on each row is read as one of:
   *all flights*, *specific flights* (`Applicable for 6E 5245`,
   `(AI-2421, AI-2429)`), *all except* (`All Flights Except 6E135`), or a
   *departure-time window* (`00:01 TO 09:00 HRS`; windows that cross
   midnight work). The most specific match wins:
   specific flights > except / window > route rate.
3. **Surcharges:** sector and flight surcharges come from the rate row
   itself. If a remark says the validity has ended, the surcharge is still
   added but flagged *"ended … – confirm"*. Surcharges limited to one
   commodity, such as `(PEF)` or `Only for FRUITS…`, are skipped. Network
   surcharges from **Settings** are added when they are active and valid on
   that date.
4. **Add-on:** the per-airline amount set in **Settings**.

A flight is marked **Check** when the app can't decide which rate applies,
for example a departure time of `0` on a route with time-based rates. It is
marked **No rate** when the route isn't in the file, and **No tariff** for
airlines whose rates aren't loaded (Akasa, SpiceJet).

## Setup (Windows, macOS or Linux)

You need Python 3.10 or newer.

```bash
python -m venv .venv
.venv\Scripts\activate            # macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
python run.py
```

Open http://localhost:5000. To let colleagues on the same network use it, run
`python run.py --host 0.0.0.0` and share `http://<your-pc-name>:5000`.

The data is kept in a single SQLite file, `instance/tariff.sqlite`. Back up
that file and you've backed up everything. To keep it somewhere else, set the
`TARIFFAPP_DB` environment variable to another path before starting.

### First-time data load

1. **Upload → Flight schedule workbook**: upload
   `ATC_AOC_Flight_Schedule_v2-2.xlsx`. This imports the flights (*Raw Data*),
   station names and hubs (*Lists*), add-on rates and network surcharges.
2. **Your existing rate history.** Either upload all your old rate-list
   `.xlsx` files on **Upload → Rate files** (you can select many at once), or
   copy everything from your existing CargoRates database in one go:

   ```bash
   flask --app tariffapp import-legacy "C:\CargoRates\CargoRates.accdb"      # Windows, needs pyodbc + Access driver
   flask --app tariffapp import-legacy cargo_rates_demo.sqlite               # or the SQLite copy
   ```

### Every ~15 days

Go to **Upload → Rate files** and upload the new file(s). That's it: every
page uses the new rates straight away. Files that are already loaded are
skipped. The effective date is read from the filename (e.g.
`8.1 6E Eff. 16th Aug 2026.xlsx`). If a filename has no date, enter it under
*Options*.

When the airline notes announce a new network-wide surcharge (fuel, festive,
ex-station…), add it under **Settings → Network surcharges** with its
validity dates.

### Command line (optional)

```bash
flask --app tariffapp load-rates "C:\CargoRates\incoming"       # a folder or individual files
flask --app tariffapp import-schedule ATC_AOC_Flight_Schedule_v2-2.xlsx
flask --app tariffapp import-legacy CargoRates.accdb
```

## Project layout

```
run.py                     start the app
tariffapp/
  __init__.py              app factory
  schema.py                database tables (the CargoRates rate tables + schedule/settings tables)
  db.py                    SQLite connection
  etl/parsers.py           reads the Air India / IndiGo Excel files (unchanged from CargoRates)
  etl/loader.py            loads a parsed file into the database
  importers.py             schedule workbook + legacy database import
  linker.py                flight → rate matching, surcharges, CPKG (replaces link_rates.py)
  views.py                 web pages
  templates/, static/      HTML + CSS (Chart.js is bundled under static/vendor)
tests/                     pytest suite: python -m pytest
```

## Notes and limits

* **No login.** Anyone who can reach the app can upload files and change
  settings. Keep it on your own PC or office network. Adding a login is a
  small change if you ever host it publicly.
* **Akasa and SpiceJet** show *No tariff* until their rate files can be read.
  Each airline's Excel layout differs, so adding one means a new reader in
  `etl/parsers.py`. Send a sample file when you have one.
* The rate tables keep exactly the CargoRates layout (one table per product
  per carrier, plus `LoadBatches`), so any queries you wrote against the
  Access database still work against `instance/tariff.sqlite`.
