# Dummy_website – ACL Dummy

Just a dummy acl website to test and track the updates and push new changes/requirements in original repo.

A Streamlit **Flight Schedule**: pick Origin and Destination to see the route, weekly
flights, flight count, status, and every flight with its product, ETD and
**CPKG (₹/kg) at 45+ and 100+ kg**. The lowest CPKG on the route in each column is shown in light red.

**CPKG = the airline's own tariff rate (45+ / 100+) + Terminal & Surcharges** from the
Allcargo rate quotation, for that flight: its own flight-number row, else its product's
row, else the route's `ALL` row. Hover a CPKG value to see its breakdown.

**Akasa** is priced from the Akasa tariff alone: its rate (surcharges are already
included) + an add-on from its OCDC sheet (origin outbound + destination inbound
general-cargo ₹/kg terminal charges). Flights the Akasa special-rates file covers
(marked SPECIAL) take its Direct AWB rate for 100+ kg, plus the same add-on.

* Everyone must sign in. Everyone who signs in sees the Flight Schedule.

## The data
The app reads these files from the `data/` folder:

| Folder | Files |
|---|---|
| `data/schedule/dgca/` | DGCA approved-schedule PDFs, one per airline. `schedule.csv` is the app's saved reading of them; it is re-made automatically when the PDFs change (takes a few minutes). |
| `data/rates/` | Airline tariffs (.xlsx): IndiGo, Air India, Akasa, SpiceJet, Star Air. Keep the airline's file name; the effective date is read from it. |
| `data/allcargo/` | The Allcargo rate quotation (.xlsx) – Terminal & Surcharges. |
| `data/special/` | The Akasa special rates (.pdf). |

**New airline tariff (every ~15 days):** sign in as `ashita`, and in the sidebar under
*Update tariff* upload the new file. The app shows what changes, line by line, against the
tariff in use (changed rates, new lines, lines not in the new file). Then choose:

* *Update only the lines in this file* – for a file with just the revised routes; every other
  line stays as it is (the newest file wins for each line it has).
* *Replace the whole tariff* – for a complete new tariff; the old file is removed.

*Publish* saves the tariffs in a private Supabase Storage bucket (not in GitHub; needs the
`[supabase]` section in Secrets, see `secrets.toml.example`). Everyone sees the new rates
at once, and the app loads them back from Supabase whenever it restarts, so they stay
until the next upload.

**To change the data:** on GitHub open the folder, click *Add file → Upload files*,
drop in the new file, delete the old one, and *Commit changes*. Streamlit Cloud
redeploys the app in about a minute with the new data.

The rate and schedule engine (`tariffapp/`) is copied from the TarrifApp repo.

## Deploy (Streamlit Community Cloud)
1. Push this repo to GitHub.
2. App → Settings → **Secrets** → paste the contents of your private `secrets.toml`
   (layout: see `secrets.toml.example`). Logins are **not** stored in the repo.
3. New/changed password: `python generate_password.py`, paste the hash into Secrets.

Local run: copy your secrets to `.streamlit/secrets.toml` (git-ignored), then `streamlit run app.py`.

## Tests
```
pip install -r requirements.txt pytest
python -m pytest tests
```
