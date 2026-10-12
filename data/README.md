# Data folder

The app loads its data from here every time it starts, and whenever a file
is added or replaced. This is how you update it: only people who can change
this repository can change the data, and the data survives app restarts.

| Folder | Put here |
|---|---|
| `schedule/` | The ATC/AOC flight-schedule workbook (`.xlsx`). If there are several, the newest one is used. Its *Raw Data*, *Lists*, *Add-on Rates* and *Network Surcharges* sheets are imported. |
| `rates/` | Every Air India / IndiGo rate-list `.xlsx` you receive. Keep the airline's file name; the effective date is read from it (e.g. `8.1 6E Eff. 16th Aug 2026.xlsx`). Keep the old files too, because they're the rate history. |

**To update from GitHub's website:** open this folder, click *Add file → Upload
files*, drop in the new file, and click *Commit changes*. Streamlit Cloud
redeploys the app in about a minute, with the new rates in place.
