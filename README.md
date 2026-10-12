# Cargo Rates

A one-page dashboard for domestic air cargo rates. Pick **From**, **To** and
the **weight**, and it shows every nonstop flight on that route, ranked by
what it will actually cost you per kg. The cheapest options are highlighted.

> All-in per kg = (air freight + surcharges + your add-on) ÷ weight

Air freight uses the right weight break for your weight, the minimum charge,
and the rule that a higher break is used when it works out cheaper. Small
shipments therefore show their true, higher, cost per kg.

It replaces the *CargoRates* Access database and the Dashboard sheet of the
*ATC/AOC Flight Schedule* workbook.

## Run it

You need Python 3.10 or newer.

```bash
pip install -r requirements.txt
streamlit run streamlit_app.py
```

It opens at http://localhost:8501. Colleagues on the same network can use
`http://<your-pc-name>:8501`.

## Keeping it up to date

Everything is in the **Update data** sidebar:

* **New rate files.** Upload each Air India or IndiGo rate list as it
  arrives, every ~15 days. Files already loaded are skipped. The effective
  date is read from the file name (e.g. `8.1 6E Eff. 16th Aug 2026.xlsx`),
  and the dashboard always uses the newest file that is in force today.
* **Flight schedule workbook.** Upload `ATC_AOC_Flight_Schedule_*.xlsx`
  whenever the schedule changes. It replaces the flights, and updates your
  add-on costs (*Add-on Rates* sheet) and network-wide surcharges such as
  fuel, SS, ex-BOM and festive (*Network Surcharges* sheet). Edit those
  sheets in Excel and re-upload, just as before.

### First-time load of your full history

To bring in all your past rate files in one go instead of uploading them one
by one:

```bash
python manage.py import-schedule ATC_AOC_Flight_Schedule_v2-2.xlsx
python manage.py load-rates "C:\CargoRates\incoming"                  # a folder of rate files
python manage.py import-legacy "C:\CargoRates\CargoRates.accdb"      # or: copy the old Access database
```

`import-legacy` on an `.accdb` needs Windows with the Microsoft Access
driver installed.

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

* **No login.** Anyone who can open the page can upload files. Keep it on
  your PC or office network, or limit viewers in the Streamlit Cloud
  settings.
* **Streamlit Community Cloud doesn't keep files.** The database
  (`instance/tariff.sqlite`) is wiped when the app restarts or you push an
  update, so you'd re-upload the schedule and rate files. For permanent use,
  run it on an office PC.
* **Akasa and SpiceJet** show *Tariff not loaded* until a reader for their
  rate files is added. Send a sample file of each.

## Project layout

```
streamlit_app.py       the dashboard
manage.py              bulk data commands
tariffapp/
  etl/parsers.py       reads the Air India / IndiGo Excel files
  etl/loader.py        stores a parsed file (every cycle kept, tagged by effective date)
  importers.py         schedule workbook + old database import
  linker.py            flight → rate matching, surcharges, cost per kg
  queries.py, db.py, schema.py
tests/                 python -m pytest
```
