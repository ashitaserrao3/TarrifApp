# Data folder

The app loads its data from here every time it starts, and whenever a file
is added or replaced. This is how you update it: only people who can change
this repository can change the data, and the data survives app restarts.

| Folder | Put here |
|---|---|
| `schedule/` | The ATC/AOC flight-schedule workbook (`.xlsx`). If there are several, the newest one is used. Its *Raw Data*, *Lists*, *Add-on Rates* and *Network Surcharges* sheets are imported. |
| `schedule/dgca/` | The DGCA approved-schedule PDFs, one per airline (e.g. `INTERGLOBE AVIATION LTD_2026.pdf`). When present, **the flights come from these** and not from the workbook's Raw Data. Each new season (summer from late March, winter from late October), replace them with the new season's files. The app reads them once and saves the result as `schedule.csv` here. |
| `history/` | Your rate history from the old CargoRates database: 32 rate files, Mar–Aug 2026 (Air India up to 20-Aug-26, IndiGo up to 16-Aug-26). Loaded once. New rate files go in `rates/`, not here. |
| `rates/` | Every airline tariff `.xlsx` you receive: IndiGo, Air India, Akasa, SpiceJet, Star Air. Keep the airline's file name; the effective date is read from it (e.g. `8.1 6E Eff. 16th Aug 2026.xlsx`). Keep the old files too, because they're the rate history. |

**To update from GitHub's website:** open this folder, click *Add file → Upload
files*, drop in the new file, and click *Commit changes*. Streamlit Cloud
redeploys the app in about a minute, with the new rates in place.

`history/CargoRates_history.sqlite.gz` was extracted from the `CargoRates.accdb`
and `cargo_rates_demo.sqlite` you supplied. The Access file cut condition
texts (the "Applicable Flight …" lists) off at 50 characters, so the full
text was taken from the demo database for the first 31 files. For the 20-Aug
Air India file, the 33 cut-off texts were restored from the workbook's
*Flight Rates* sheet or the 12-Aug file. The result matches the workbook's
*Flight Rates* sheet for all 1,630 flights.
