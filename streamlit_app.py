"""
ATC/AOC Flight Schedule - the Excel workbook, live in the browser.

    streamlit run streamlit_app.py

The Dashboard tab reproduces the workbook's Dashboard sheet (pick Origin and
Destination, see every flight with its CPKG); the other tabs are the
workbook's other sheets, computed live from the loaded schedule and rate
files.

Data comes from the data/ folder (synced automatically - see data/README.md).
Everyone can view and interact; only the admin can upload, after signing in
with the password set as `admin_password` in .streamlit/secrets.toml (or the
TARIFFAPP_ADMIN_PASSWORD environment variable). With no password set,
uploading in the app is switched off.
"""

import datetime as dt
import hmac
import html
import os
import re

import pandas as pd
import streamlit as st

from tariffapp import database_path, db, queries, sync
from tariffapp.importers import DAYS
from tariffapp.linker import STATUS_CHECK, STATUS_NO_RATE, STATUS_NO_TARIFF, STATUS_OK, TariffEngine

st.set_page_config(page_title="Flight Schedule", page_icon="✈", layout="wide")

# Colours taken from the workbook's own formatting
NAVY, BLUE, GREEN_DARK = "#1F4E78", "#2E74B5", "#375623"
BAND, INPUT_YELLOW, STRIPE, GREY_TEXT, MSG_BROWN = "#DCE6F1", "#FFF2CC", "#F2F2F2", "#595959", "#833C0C"
STATUS_FILL = {"ACTIVE": "#C6E0B4", "NO SERVICE": "#F8CBAD", "SAME CITY": "#FFF2CC"}
CHECK_AMBER = "#FFE699"

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
.xl-table td.check {{ background: {CHECK_AMBER} !important; }}
.xl-table td.muted {{ color: #808080; }}
.xl-table sup {{ color: #9C5700; font-size: 10px; font-weight: 400; }}
.xl-note {{ color: {GREY_TEXT}; font-size: 11px; margin-top: 10px; }}
.xl-sheet-head {{ color: #fff; font-weight: 700; padding: 6px 10px; margin-bottom: 6px; border-radius: 2px; }}
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
    """Store an admin upload in the data folder under its own name, so it is synced like any other file."""
    folder = os.path.join(sync.data_dir(), sub)
    os.makedirs(folder, exist_ok=True)
    name = re.sub(r'[\\/:*?"<>|]+', "_", os.path.basename(f.name)).strip() or "upload.xlsx"
    with open(os.path.join(folder, name), "wb") as out:
        out.write(f.getbuffer())
    return name


def admin_password():
    try:
        pw = st.secrets.get("admin_password")
    except Exception:  # noqa: BLE001 - no secrets file at all
        pw = None
    return pw or os.environ.get("TARIFFAPP_ADMIN_PASSWORD")


@st.cache_resource(show_spinner="Loading the latest data…")
def sync_data(signature):
    """Runs once per change of the data folder (signature), shared by every visitor."""
    c = db.connect(database_path())
    db.init_schema(c)
    try:
        return sync.sync_data_folder(c)
    finally:
        c.close()


def show_sheet(df, height=None, **config):
    """Show a sheet's table the way Excel does: empty cells blank (not 'None'), whole numbers without
    decimals, numbers right-aligned."""
    def fmt(v):
        if v is None or (isinstance(v, float) and pd.isna(v)):
            return ""
        if isinstance(v, float):
            return f"{v:g}" if v == int(v) else f"{v:.2f}"
        return str(v)
    numeric = [c for c in df.columns if pd.api.types.is_numeric_dtype(df[c])]
    cfg = {c: st.column_config.TextColumn(c, alignment="right") for c in numeric}
    cfg.update(config)
    kwargs = {"height": height} if height else {}
    st.dataframe(df.map(fmt), hide_index=True, width="stretch", column_config=cfg, **kwargs)


def sheet_heading(text, colour):
    st.html(SHEET_CSS + f'<div class="xl xl-sheet-head" style="background:{colour}">{esc(text)}</div>')


conn = get_conn()
today = dt.date.today()

# ---------------------------------------------------------------------------
# Data: synced from the data folder; uploads only for the signed-in admin
# ---------------------------------------------------------------------------

sync_result = sync_data(sync.folder_signature())
ss = st.session_state
password = admin_password()

with st.sidebar:
    st.caption("**Rates in use**")
    for carrier, b in queries.latest_batches(conn).items():
        st.caption(f"{carrier}: eff. {dmy(b['EffectiveDate'])}" if b else f"{carrier}: none loaded")
    n_flights = conn.execute("SELECT COUNT(*) FROM Flights").fetchone()[0]
    st.caption(f"Schedule: {n_flights:,} flights")
    st.divider()

    if ss.get("is_admin"):
        st.subheader("Update data")
        with st.form("rates", clear_on_submit=True, border=False):
            files = st.file_uploader("New Air India / IndiGo rate files", type=["xlsx"], accept_multiple_files=True,
                                     help="Saved into data/rates and loaded. Files already loaded are skipped.")
            if st.form_submit_button("Load rate files", width="stretch", key="load_rates") and files:
                for f in files:
                    save_to_data_folder(f, "rates")
                st.cache_resource.clear()
                st.rerun()
        with st.form("schedule", clear_on_submit=True, border=False):
            sched = st.file_uploader("Flight schedule workbook", type=["xlsx", "xlsm"],
                                     help="Saved into data/schedule and imported (Raw Data, Lists, Add-on Rates, "
                                          "Network Surcharges).")
            if st.form_submit_button("Load schedule", width="stretch", key="load_schedule") and sched:
                save_to_data_folder(sched, "schedule")
                st.cache_resource.clear()
                st.rerun()
        if st.button("Reload data folder", width="stretch", key="reload"):
            st.cache_resource.clear()
            st.rerun()
        if sync_result["schedule"] or sync_result["rates_loaded"]:
            loaded = [f"schedule {sync_result['schedule']}"] if sync_result["schedule"] else []
            loaded += sync_result["rates_loaded"]
            st.success("Loaded: " + "; ".join(loaded))
        for e in sync_result["errors"]:
            st.error(e)
        st.caption("On Streamlit Cloud, files uploaded here last until the app restarts. To keep them, also add "
                   "them to the data folder in GitHub.")
        if st.button("Sign out", key="sign_out"):
            ss["is_admin"] = False
            st.rerun()
    else:
        with st.expander("🔒 Admin"):
            if not password:
                st.caption("Uploading is switched off here. The data is updated from the data folder.")
            else:
                with st.form("login", border=False):
                    pw = st.text_input("Password", type="password", key="admin_pw")
                    if st.form_submit_button("Sign in", key="sign_in"):
                        if hmac.compare_digest(pw.encode(), str(password).encode()):
                            ss["is_admin"] = True
                            st.rerun()
                        else:
                            st.error("Wrong password.")

hubs, stations = queries.stations(conn)
if not stations:
    st.html(SHEET_CSS + '<div class="xl xl-title">✈&nbsp; FLIGHT &nbsp;SCHEDULE</div>')
    st.info("No data yet. Add the flight schedule workbook to data/schedule and your Air India / IndiGo rate "
            "files to data/rates (see data/README.md), or sign in as admin in the sidebar and upload them.")
    st.stop()

eng = TariffEngine(conn, today)
names = {s["Code"]: s["Name"] or "" for s in stations}
hub_codes = [s["Code"] for s in hubs] or list(names)
all_codes = list(names)
airline_name = {code: a.get("Name") or code for code, a in eng.airlines.items()}

tabs = st.tabs(["Dashboard", "Add-on Rates", "Raw Data", "OD Pair Summary", "Network Surcharges", "Flight Rates",
                "Rate Exceptions"])

# ---------------------------------------------------------------------------
# Dashboard sheet
# ---------------------------------------------------------------------------

with tabs[0]:
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

    flights = [dict(r) for r in conn.execute(
        "SELECT * FROM Flights WHERE Origin=? AND Destination=? ORDER BY FlightID", (origin, dest))]
    results = eng.price_all(flights) if origin != dest else []
    weekly = sum(queries.weekly_frequency(r.flight) for r in results)
    status = "SAME CITY" if origin == dest else ("ACTIVE" if weekly else "NO SERVICE")

    ig, ai = eng.batch("IndiGo"), eng.batch("Air India")
    rates_line = (f"Rates: IndiGo eff. {dmy(ig['EffectiveDate']) if ig else '–'}  |  "
                  f"Air India eff. {dmy(ai['EffectiveDate']) if ai else '–'}  |  Linked {dmy(today)}")
    msg = {"SAME CITY": "⚠&nbsp; Please select two different cities as Origin and Destination.",
           "NO SERVICE": "✈&nbsp; No nonstop flights currently operate on this route. "
                         "Please try a different Origin / Destination combination."}.get(status, "")

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
        rows_html.append(
            f"<tr><td>{esc(f['Airline'])}</td><td class='c'>{esc(f['FlightNo'])}</td>"
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
    its breakdown. Surcharges: see 'Flight Rates' (per flight) and 'Network Surcharges' (airline-wide); add-ons are
    set per airline on 'Add-on Rates'. Amber product = check 'Rate Exceptions'. A small <b>N</b> means the tariff has
    no 45+/100+ rate on that route, so the N rate is used.</div>
</div>""")

# ---------------------------------------------------------------------------
# The other sheets, computed live
# ---------------------------------------------------------------------------

with tabs[1]:
    sheet_heading("Add-on Rates", NAVY)
    df = pd.read_sql_query("SELECT Code AS [Airline code (as in Raw Data)], Name AS Airline, "
                           "AddOnPerKg AS [Add-on ₹/kg], COALESCE(AddOnIncludes, 'not set') AS Includes "
                           "FROM Airlines ORDER BY Code", conn)
    show_sheet(df)
    st.caption("To change an add-on, edit the *Add-on Rates* sheet in your workbook and replace the workbook in the "
               "data folder. CPKG on the Dashboard updates straight away.")

all_flights = pd.read_sql_query("SELECT * FROM Flights ORDER BY FlightID", conn)

with tabs[2]:
    sheet_heading("Raw Data", "#404040")
    raw = all_flights[["Origin", "Destination"]].copy()
    raw["OD Pair"] = all_flights["Origin"] + "-" + all_flights["Destination"]
    raw = pd.concat([raw, all_flights[["Airline", "FlightNo", "DepTime", *DAYS]]], axis=1).rename(
        columns={"FlightNo": "Flight No", "DepTime": "Dep Time"})
    raw["Weekly Freq"] = all_flights[DAYS].sum(axis=1)
    show_sheet(raw, height=560)
    st.caption(f"{len(raw):,} nonstop flights. Use the search icon at the top right of the table to find a flight.")

with tabs[3]:
    sheet_heading("OD Pair Summary", "#404040")

    def carrier_label(row):
        if row["Airline"] == "AIRINDIA":
            return "Air India Express" if str(row["FlightNo"]).upper().startswith("IX") else "Air India"
        return airline_name.get(row["Airline"], row["Airline"])

    od = all_flights.assign(**{"OD Pair": all_flights["Origin"] + "-" + all_flights["Destination"],
                               "Airline ": all_flights.apply(carrier_label, axis=1),
                               "Total Weekly Flights": all_flights[DAYS].sum(axis=1)})
    od = (od.groupby(["OD Pair", "Airline "], sort=False)["Total Weekly Flights"].sum().reset_index()
          .rename(columns={"Airline ": "Airline"}))
    show_sheet(od, height=560)

with tabs[4]:
    sheet_heading("Network Surcharges", "#C00000")
    ns = pd.read_sql_query(
        "SELECT Carrier AS Airline, AppliesTo AS [Applies To], Station AS [Station / Route], RatePerKg AS [Rs per kg], "
        "ValidFrom AS [Valid From], ValidTo AS [Valid To], Reason AS [Reason / Source], "
        "CASE WHEN Active = 1 THEN 'Y' ELSE 'N' END AS [Active (Y/N)], Label AS [Dashboard Label] "
        "FROM NetworkSurcharges ORDER BY SurchargeID", conn)
    show_sheet(ns)
    st.caption("Surcharges from the tariff notes / footnotes that apply across many routes. Route- and flight-specific "
               "surcharges are read from the rate files automatically. Edit this sheet in your workbook and replace the "
               "workbook in the data folder to change them.")

priced = eng.price_all([dict(r) for r in all_flights.to_dict("records")])

with tabs[5]:
    sheet_heading("Flight Rates", BLUE)
    fr = pd.DataFrame([{
        "Origin": r.flight["Origin"], "Destination": r.flight["Destination"], "Airline": r.flight["Airline"],
        "Flight No": r.flight["FlightNo"], "Dep Time": r.flight["DepTime"], "Product": r.product,
        "Effective Date": dmy(r.effective_date) or None, "Min": r.rates["Min"], "N": r.rates["N"],
        "45+": r.rates["45"], "100+": r.rates["100"], "300+": r.rates["300"], "500+": r.rates["500"],
        "1000+": r.rates["1000"], "Status": r.status,
        "Tariff Condition": r.condition.text if r.condition and r.condition.text else None,
        "Match Note": r.match_note, "Surcharge Rs/kg": r.surcharge_total if r.rate_row else None,
        "Surcharge Summary": r.surcharge_summary or None,
    } for r in priced])
    for col in ("Min", "N", "45+", "100+", "300+", "500+", "1000+", "Surcharge Rs/kg"):
        fr[col] = pd.to_numeric(fr[col], errors="coerce")
    counts = fr["Status"].value_counts()
    st.caption(" · ".join(f"{k}: {v:,}" for k, v in counts.items()))
    show_sheet(fr, height=560, Status=st.column_config.TextColumn(width="medium"))

with tabs[6]:
    sheet_heading("Rate Exceptions", "#C55A11")
    ex = [r for r in priced if r.status in (STATUS_CHECK, STATUS_NO_RATE)]
    if ex:
        show_sheet(pd.DataFrame([{
            "Origin": r.flight["Origin"], "Destination": r.flight["Destination"], "Airline": r.flight["Airline"],
            "Flight No": r.flight["FlightNo"], "Dep Time": r.flight["DepTime"], "Status": r.status,
            "Note": r.match_note, "Tariff Condition": r.condition.text if r.condition else None,
        } for r in ex]))
    else:
        st.success("No exceptions – every flight with a loaded tariff found its rate.")
    ok = sum(r.status == STATUS_OK for r in priced)
    st.caption(f"{ok:,} of {len(priced):,} flights priced OK.")
