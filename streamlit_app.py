"""
ATC/AOC Flight Schedule - the Excel workbook, live in the browser.

    streamlit run streamlit_app.py

Shows the workbook's Dashboard sheet: pick Origin and Destination and see
every flight with its CPKG, computed live from the loaded schedule and rate
files.

Data comes from the data/ folder (synced automatically - see data/README.md).
Anyone using the app can also upload new rate files or the schedule workbook
from the sidebar.
"""

import datetime as dt
import html
import os
import re

import streamlit as st

from tariffapp import database_path, db, queries, sync
from tariffapp.linker import STATUS_CHECK, STATUS_NO_RATE, STATUS_NO_TARIFF, TariffEngine

st.set_page_config(page_title="Flight Schedule", page_icon="✈", layout="wide")

# Colours taken from the workbook's own formatting
NAVY, BLUE, GREEN_DARK = "#1F4E78", "#2E74B5", "#375623"
BAND, INPUT_YELLOW, STRIPE, GREY_TEXT, MSG_BROWN = "#DCE6F1", "#FFF2CC", "#F2F2F2", "#595959", "#833C0C"
STATUS_FILL = {"ACTIVE": "#C6E0B4", "NO SERVICE": "#F8CBAD", "SAME CITY": "#FFF2CC"}
CHECK_AMBER = "#FFE699"
LOWEST_RED = "#FFC7CE"  # Excel's "light red fill"

SHEET_CSS = f"""
<style>
.xl {{ font-family: Aptos Narrow, "Arial Narrow", Calibri, Arial, sans-serif; color: #000; }}
.xl-title {{ background: {NAVY}; color: #fff; font-weight: 700; font-size: 24px; text-align: center;
             padding: 10px 0; letter-spacing: .08em; }}
.xl-rates {{ color: {GREY_TEXT}; font-size: 12px; text-align: right; margin: 2px 0 6px; }}
.xl-band {{ display: grid; grid-template-columns: 4fr 2fr 2fr 3fr; background: {BAND}; }}
.xl-band > div {{ padding: 4px 10px 6px; min-width: 0; }}
.xl-band .k {{ font-size: 12px; font-weight: 700; }}
.xl-band .v {{ color: {NAVY}; font-weight: 700; font-size: 21px; line-height: 1.25; }}
.xl-band .route {{ font-size: 17px; overflow-wrap: anywhere; }}
.xl-band .st {{ color: #000; font-size: 18px; }}
.xl-msg {{ color: {MSG_BROWN}; font-weight: 700; text-align: center; padding: 10px 0 4px; font-size: 15px; }}
.xl-wrap {{ overflow-x: auto; margin-top: 10px; }}
.xl-table {{ border-collapse: collapse; width: 100%; font-size: 14px; }}
.xl-table th {{ background: {BLUE}; color: #fff; font-weight: 700; text-align: center; padding: 5px 8px;
                border: 1px solid #fff; white-space: nowrap; }}
.xl-table th.cpkg {{ background: {GREEN_DARK}; }}
.xl-table td {{ padding: 5px 8px; border-bottom: 1px solid #E7E7E7; white-space: nowrap; }}
.xl-table tbody tr:nth-child(even) td {{ background: {STRIPE}; }}
.xl-table td.c {{ text-align: center; }}
.xl-table td.cpkg {{ text-align: center; color: {GREEN_DARK}; font-weight: 700; font-size: 15px; }}
.xl-table tr.lowest td {{ background: {LOWEST_RED} !important; }}
.xl-table td.check {{ background: {CHECK_AMBER} !important; }}
.xl-table td.muted {{ color: #808080; }}
.xl-table sup {{ color: #9C5700; font-size: 10px; font-weight: 400; }}
.xl-note {{ color: {GREY_TEXT}; font-size: 11px; margin-top: 10px; }}
</style>
"""

WIDGET_CSS = f"""
<style>
/* Origin / Destination drop-downs styled like the workbook's yellow input cells */
.st-key-origin [role="group"], .st-key-dest [role="group"],
.st-key-origin div[data-baseweb="select"] > div, .st-key-dest div[data-baseweb="select"] > div {{
  background: {INPUT_YELLOW}; border-radius: 0;
}}
.st-key-origin input, .st-key-dest input {{ color: {NAVY}; font-weight: 700; font-size: 16px; }}
.xl-label {{ font-weight: 700; text-align: right; font-size: 15px; }}
.xl-name {{ color: {GREY_TEXT}; font-size: 15px; }}
</style>
"""


def get_conn():
    conn = db.connect(database_path())
    if not st.session_state.get("_schema_ok"):
        db.init_schema(conn)
        st.session_state["_schema_ok"] = True
    return conn


def dmy(v):
    if not v:
        return ""
    if isinstance(v, str):
        v = dt.date.fromisoformat(v[:10])
    return v.strftime("%d-%b-%y")


def esc(v):
    return html.escape("" if v is None else str(v))


def num(v):
    if v is None:
        return ""
    return f"{v:g}" if float(v) == int(v) else f"{v:.2f}"


def save_to_data_folder(f, sub):
    """Store an upload in the data folder under its own name, so it is synced like any other file."""
    folder = os.path.join(sync.data_dir(), sub)
    os.makedirs(folder, exist_ok=True)
    name = re.sub(r'[\\/:*?"<>|]+', "_", os.path.basename(f.name)).strip() or "upload.xlsx"
    with open(os.path.join(folder, name), "wb") as out:
        out.write(f.getbuffer())
    return name



@st.cache_resource(show_spinner="Loading the latest data…")
def sync_data(signature):
    """Runs once per change of the data folder (signature), shared by every visitor."""
    c = db.connect(database_path())
    db.init_schema(c)
    try:
        return sync.sync_data_folder(c)
    finally:
        c.close()


conn = get_conn()
today = dt.date.today()

# ---------------------------------------------------------------------------
# Data: synced from the data folder; anyone can upload from the sidebar
# ---------------------------------------------------------------------------

sync_result = sync_data(sync.folder_signature())
ss = st.session_state

with st.sidebar:
    st.caption("**Rates in use**")
    for carrier, b in queries.latest_batches(conn).items():
        st.caption(f"{carrier}: eff. {dmy(b['EffectiveDate'])}" if b else f"{carrier}: none loaded")
    sched_info = queries.schedule_info(conn, today)
    st.caption(f"Schedule: {sched_info['label']} · {sched_info['flights']:,} flights")
    if sched_info["expired"]:
        st.warning(f"The {sched_info['label']} schedule ended on {dmy(sched_info['valid_to'])}. Flights are shown "
                   "as on its last day. Upload the new season's DGCA schedule PDFs below.")
    st.divider()

    st.subheader("Update data")
    with st.form("rates", clear_on_submit=True, border=False):
        files = st.file_uploader("New airline rate files", type=["xlsx"], accept_multiple_files=True,
                                 help="IndiGo, Air India, Akasa, SpiceJet or Star Air tariff (.xlsx). Saved into data/rates and loaded; files already loaded are skipped.")
        if st.form_submit_button("Load rate files", width="stretch", key="load_rates") and files:
            for f in files:
                save_to_data_folder(f, "rates")
            ss["_just_uploaded"] = True
            st.cache_resource.clear()
            st.rerun()
    with st.form("schedule", clear_on_submit=True, border=False):
        scheds = st.file_uploader("Flight schedule (DGCA PDFs or workbook)", type=["pdf", "xlsx", "xlsm"],
                                  accept_multiple_files=True,
                                  help="DGCA approved-schedule PDFs (one per airline) give the flights; reading them "
                                       "takes a few minutes. The workbook gives Lists, Add-on Rates and Network "
                                       "Surcharges.")
        if st.form_submit_button("Load schedule", width="stretch", key="load_schedule") and scheds:
            for f in scheds:
                save_to_data_folder(f, "schedule/dgca" if f.name.lower().endswith(".pdf") else "schedule")
            ss["_just_uploaded"] = True
            st.cache_resource.clear()
            st.rerun()
    if st.button("Reload data folder", width="stretch", key="reload"):
        st.cache_resource.clear()
        st.rerun()
    # Say what was loaded only to the person who just uploaded, not to every visitor.
    if ss.pop("_just_uploaded", False) and (sync_result["schedule"] or sync_result["dgca"]
                                            or sync_result["rates_loaded"] or sync_result["history"]):
        loaded = [f"schedule {sync_result['schedule']}"] if sync_result["schedule"] else []
        loaded += [sync_result["dgca"]] if sync_result["dgca"] else []
        loaded += [f"history {h}" for h in sync_result["history"]] + sync_result["rates_loaded"]
        st.success("Loaded: " + "; ".join(loaded))
    for e in sync_result["errors"]:
        st.error(e)
    st.caption("On Streamlit Cloud, files uploaded here last until the app restarts. To keep them, also add "
               "them to the data folder in GitHub.")

hubs, stations = queries.stations(conn)
if not stations:
    st.html(SHEET_CSS + '<div class="xl xl-title">✈&nbsp; FLIGHT &nbsp;SCHEDULE</div>')
    st.info("No data yet. Add the flight schedule workbook to data/schedule and your Air India / IndiGo rate "
            "files to data/rates (see data/README.md), or upload them from the sidebar.")
    st.stop()

eng = TariffEngine(conn, today)
names = {s["Code"]: s["Name"] or "" for s in stations}
hub_codes = [s["Code"] for s in hubs] or list(names)
all_codes = queries.destinations(conn, hub_codes) or list(names)

if not ss.get("_from_link"):  # a shared link like ?from=DEL&to=BOM opens that route once per session
    ss["_from_link"] = True
    if st.query_params.get("from", "").upper() in hub_codes:
        ss["origin"] = st.query_params["from"].upper()
    if st.query_params.get("to", "").upper() in all_codes:
        ss["dest"] = st.query_params["to"].upper()
if ss.get("origin") not in hub_codes:
    ss["origin"] = "DEL" if "DEL" in hub_codes else hub_codes[0]
if ss.get("dest") not in all_codes:
    ss["dest"] = "CJB" if "CJB" in all_codes else all_codes[0]

st.html(SHEET_CSS + WIDGET_CSS + '<div class="xl xl-title">✈&nbsp; FLIGHT &nbsp;SCHEDULE</div>')
c = st.columns([1.1, 1.1, 2, 1.4, 1.1, 2], vertical_alignment="center")
c[0].html('<div class="xl xl-label">Origin:</div>')
origin = c[1].selectbox("Origin", hub_codes, key="origin", label_visibility="collapsed")
c[2].html(f'<div class="xl xl-name">{esc(names.get(origin))}</div>')
c[3].html('<div class="xl xl-label">Destination:</div>')
dest = c[4].selectbox("Destination", all_codes, key="dest", label_visibility="collapsed")
c[5].html(f'<div class="xl xl-name">{esc(names.get(dest))}</div>')
st.query_params.from_dict({"from": origin, "to": dest})

flights = queries.route_flights(conn, origin, dest, today) if origin != dest else []
results = eng.price_all(flights)
weekly = sum(queries.weekly_frequency(r.flight) for r in results)
status = "SAME CITY" if origin == dest else ("ACTIVE" if weekly else "NO SERVICE")

sched_note = sched_info["label"] + (f" (as on {dmy(sched_info['date'])})"
                                   if sched_info["date"] and sched_info["date"] != today else "")
rate_effs = " · ".join(f"{c} {dmy(b['EffectiveDate'])}" for c in queries.CARRIER_TABLES
                      if (b := eng.batch(c)))
rates_line = f"Rates eff.: {rate_effs or '–'}  |  Schedule: {sched_note}  |  Linked {dmy(today)}"
msg = {"SAME CITY": "⚠&nbsp; Please select two different cities as Origin and Destination.",
       "NO SERVICE": "✈&nbsp; No nonstop flights currently operate on this route. "
                     "Please try a different Origin / Destination combination."}.get(status, "")

def lowest_key(r):
    """The CPKG a row is compared on: 100+ kg, else 45+ kg."""
    return r.cpkg(100)[0] if r.cpkg(100)[0] is not None else r.cpkg(45)[0]


priced = [lowest_key(r) for r in results if lowest_key(r) is not None]
lowest = min(priced) if priced else None

rows_html = []
for r in results:
    f = r.flight
    if r.status == STATUS_NO_TARIFF:
        product, pcls = "No tariff", "c muted"
    elif r.status == STATUS_NO_RATE:
        product, pcls = "No rate", "c muted"
    else:
        product, pcls = r.product or "", "c check" if r.status == STATUS_CHECK else "c"
    cells = []
    for brk in (45, 100):
        v, used = r.cpkg(brk)
        if v is None:
            cells.append('<td class="cpkg"></td>')
            continue
        rate, _ = r.rate_at(brk)
        tip = (f"{num(rate)} tariff ({used}) + {num(r.surcharge_total)} surcharge "
               f"[{r.surcharge_summary or 'none'}] + {num(r.addon or 0)} add-on")
        mark = f"<sup>{used}</sup>" if used != f"{brk}+" else ""
        cells.append(f'<td class="cpkg" title="{esc(tip)}">{num(v)}{mark}</td>')
    tr = '<tr class="lowest" title="Lowest CPKG on this route">' \
        if lowest is not None and lowest_key(r) == lowest else "<tr>"
    rows_html.append(
        f"{tr}<td>{esc(f['Airline'])}</td><td class='c'>{esc(f['FlightNo'])}</td>"
        f"<td class='{pcls}' title='{esc(r.match_note)}'>{esc(product)}</td>"
        f"<td class='c'>{esc(f['DepTime'])}</td>{''.join(cells)}</tr>")

route = f"{esc(names.get(origin) or origin)} ({origin})&nbsp; → &nbsp;{esc(names.get(dest) or dest)} ({dest})"
table = ""
if rows_html:
    table = f"""
<div class="xl-wrap"><table class="xl-table">
  <thead>
<tr><th rowspan="2">AIRLINE</th><th rowspan="2">FLT NO.</th><th rowspan="2">PRODUCT</th><th rowspan="2">ETD</th>
    <th class="cpkg" colspan="2">CPKG &nbsp;(₹/kg)</th></tr>
<tr><th class="cpkg">45+ kg</th><th class="cpkg">100+ kg</th></tr>
  </thead>
  <tbody>{''.join(rows_html)}</tbody>
</table></div>"""
st.html(SHEET_CSS + f"""
<div class="xl">
  <div class="xl-rates">{esc(rates_line)}</div>
  <div class="xl-band">
<div><div class="k">ROUTE</div><div class="v route">{route}</div></div>
<div><div class="k">WEEKLY FLIGHTS</div><div class="v">{weekly}</div></div>
<div><div class="k">FLIGHT COUNT</div><div class="v">{len(results)}</div></div>
<div style="background:{STATUS_FILL[status]}"><div class="k">STATUS</div><div class="v st">{status}</div></div>
  </div>
  {f'<div class="xl-msg">{msg}</div>' if msg else ''}
  {table}
  <div class="xl-note">CPKG (₹/kg) = tariff rate + surcharge + add-on (X-ray/TSP/handling). Hover a CPKG value to see
its breakdown. Amber product = check the rate condition (hover it to see why). A small <b>N</b> means the tariff
has no 45+/100+ rate on that route, so the N rate is used. <span style="background:{LOWEST_RED};padding:0 4px">Red
row</span> = lowest CPKG on the route (100+ kg).</div>
</div>""")
