# ATC/AOC Flight Schedule — live in the browser

The **Dashboard** sheet of your *ATC/AOC Flight Schedule* workbook as an
interactive web page. It looks and works like the sheet: pick **Origin** and
**Destination** from the yellow drop-downs to see the route, weekly flights,
flight count, status, and every flight with its product, ETD and
**CPKG (₹/kg) at 45+ and 100+ kg**. Hover a CPKG value to see its breakdown
(tariff + surcharge + add-on).

Rates come from your Air India and IndiGo rate files, matched to each flight
the same way the workbook's *Flight Rates* sheet does (all 1,630 flights
match).

## Run it

You need Python 3.10 or newer.

```bash
pip install -r requirements.txt
streamlit run streamlit_app.py
```

It opens at http://localhost:8501. Colleagues on the same network can use
`http://<your-pc-name>:8501`.

The page address always holds the current route, e.g.
`http://localhost:8501/?from=DEL&to=CJB`. Bookmark it, or send it to
a colleague to open the same view.

## Who can change the data

Everyone who opens the app can **view and interact**: pick routes, switch
sheets, sort and search. **Only you update the data**, in one of two ways.

### 1. From the backend: the `data/` folder (recommended)

```
data/schedule/   the ATC/AOC flight-schedule workbook
data/rates/      every new Air India / IndiGo rate-list .xlsx
data/history/    rate history from the old CargoRates database (32 files, Mar–Aug 2026)
```

The app loads anything new in this folder when it starts or when a file
changes. A rate file already in the database is skipped, and the workbook
is only re-imported when its contents change. To update:

* **On GitHub's website:** open `data/rates` (or `data/schedule`), click
  *Add file → Upload files*, drop in the file, and click *Commit changes*.
  Streamlit Cloud picks it up within about a minute.
* **On your PC:** copy the file into the folder. Then commit and push it, or
  run `python manage.py sync`.

Only people who can change your GitHub repository can change the data. Because
the files live in the repository, the data survives app restarts and
redeploys on Streamlit Community Cloud.

### 2. Admin sign-in inside the app

Set a password, and you'll see a **🔒 Admin** box in the sidebar. Signing in
shows the upload buttons. Visitors never see them.

* **On your PC:** copy `.streamlit/secrets.toml.example` to
  `.streamlit/secrets.toml` and set `admin_password`. Git ignores this file,
  so the password is never pushed.
* **On Streamlit Cloud:** open the app's *Settings → Secrets* and add
  `admin_password = "your password"`.

With no password set, uploading inside the app is switched off completely.
Files uploaded this way are saved into `data/`. On Streamlit Cloud they last
only until the app restarts, so for anything permanent use option 1.

### Your history is already in

`data/history/` holds every rate cycle from your old CargoRates database, up
to Air India 20-Aug-26 and IndiGo 16-Aug-26. From now on, just add each new
rate file to `data/rates/`. To import another old database yourself:
`python manage.py import-legacy "C:\CargoRates\CargoRates.accdb"` (needs
Windows with the Microsoft Access driver).

## How each flight gets its rate

* The newest rate file in force today is used for each airline.
* Only general cargo counts: IndiGo GCR / LEAN, Air India GCR / PRIME.
  Each rate row's condition is read as *all flights*, *these flights*,
  *all except these flights*, or a *departure-time window* (windows that
  cross midnight work). The most specific match wins.
* Surcharges come from the rate file itself (sector and flight) plus the
  network surcharges in your schedule workbook, checked against their dates.
  A surcharge whose validity has ended is still added, with a note to
  confirm it with the airline. Surcharges that apply to one commodity only
  are skipped.
* Flights the app can't price confidently, such as a departure time of `0`
  on a route with time-based rates, appear at the bottom with a note.

These rules reproduce the old workbook's *Flight Rates* sheet exactly for
all 1,630 flights.

## Good to know

* **Viewing is open to anyone with the link.** To limit who can view, make
  the app private in the Streamlit Cloud settings and invite people by email.
* **Streamlit Community Cloud doesn't keep files it wasn't deployed with.**
  The database is rebuilt from the `data/` folder after a restart. The first
  visitor after a restart waits while it loads, which can take a minute with
  many rate files. That's why updates should go into `data/` in GitHub.
* **Akasa and SpiceJet** show *Tariff not loaded* until a reader for their
  rate files is added. Send a sample file of each.

## Project layout

```
streamlit_app.py       the app (the Dashboard sheet)
manage.py              data commands (sync, load-rates, import-schedule, import-legacy)
data/                  your schedule workbook and rate files (see data/README.md)
tariffapp/
  etl/parsers.py       reads the Air India / IndiGo Excel files
  etl/loader.py        stores a parsed file (every cycle kept, tagged by effective date)
  importers.py         schedule workbook + old database import
  linker.py            flight → rate matching, surcharges, cost per kg
  sync.py              loads new files from the data/ folder
  queries.py, db.py, schema.py
tests/                 python -m pytest
```
