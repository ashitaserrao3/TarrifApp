"""
Cargo Rates - one-page dashboard.

    streamlit run streamlit_app.py

Pick a route and a weight; see every nonstop flight ranked by all-in cost
per kg (tariff + surcharges + your add-on), cheapest first. New rate files
and the schedule workbook are uploaded from the sidebar.
"""

import datetime as dt
import os
import tempfile

import pandas as pd
import streamlit as st

from tariffapp import database_path, db, queries
from tariffapp.etl.loader import load_rate_file
from tariffapp.importers import DAYS, import_schedule_workbook
from tariffapp.linker import STATUS_CHECK, STATUS_NO_RATE, STATUS_OK

st.set_page_config(page_title="Cargo Rates", page_icon="✈", layout="wide")


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


def rs(v):
    return f"₹{v:,.0f}"


def save_upload(f):
    folder = os.path.join(os.path.dirname(database_path()), "uploads")
    os.makedirs(folder, exist_ok=True)
    fd, path = tempfile.mkstemp(suffix=".xlsx", dir=folder, prefix=dt.datetime.now().strftime("%Y%m%d-%H%M%S_"))
    with os.fdopen(fd, "wb") as out:
        out.write(f.getbuffer())
    return path


conn = get_conn()
today = dt.date.today()

# ---------------------------------------------------------------------------
# Sidebar: keep the data up to date
# ---------------------------------------------------------------------------

with st.sidebar:
    st.header("Update data")
    with st.form("rates", clear_on_submit=True, border=False):
        files = st.file_uploader("New Air India / IndiGo rate files", type=["xlsx"], accept_multiple_files=True,
                                 help="Upload each new rate list as it arrives. Files already loaded are skipped.")
        if st.form_submit_button("Load rate files", width="stretch") and files:
            for f in files:
                r = load_rate_file(conn, save_upload(f), filename=f.name)
                msg = f"{f.name}: {r['message']}"
                {"ok": st.success, "skipped": st.info}.get(r["status"], st.error)(msg)
                for w in r["warnings"]:
                    st.warning(f"{f.name}: {w}")
    with st.form("schedule", clear_on_submit=True, border=False):
        sched = st.file_uploader("Flight schedule workbook", type=["xlsx", "xlsm"],
                                 help="The ATC/AOC schedule workbook. Replaces the flights, and updates add-on "
                                      "rates and network surcharges from its sheets.")
        if st.form_submit_button("Load schedule", width="stretch") and sched:
            try:
                s = import_schedule_workbook(conn, save_upload(sched), filename=sched.name)
                st.success(f"Loaded {s['flights']} flights and {s['surcharges']} network surcharges.")
            except Exception as e:  # noqa: BLE001 - show the problem to the user
                conn.rollback()
                st.error(f"{sched.name}: {e}")

    st.divider()
    st.caption("**Rates in use**")
    for carrier, b in queries.latest_batches(conn).items():
        st.caption(f"{carrier}: eff. {dmy(b['EffectiveDate'])}" if b else f"{carrier}: none loaded")
    n_flights = conn.execute("SELECT COUNT(*) FROM Flights").fetchone()[0]
    st.caption(f"Schedule: {n_flights:,} flights")

# ---------------------------------------------------------------------------
# The sheet
# ---------------------------------------------------------------------------

st.title("✈ Cargo rates")
hubs, stations = queries.stations(conn)
if not stations:
    st.info("Nothing loaded yet. Use **Update data** in the sidebar: first the flight schedule workbook, "
            "then your Air India and IndiGo rate files.")
    st.stop()

names = {s["Code"]: s["Name"] for s in stations}
hub_codes = [s["Code"] for s in hubs] or list(names)
all_codes = list(names)
ss = st.session_state
if not ss.get("_from_link"):  # open a shared link like ?from=DEL&to=BOM&kg=250 once per session
    ss["_from_link"] = True
    qp = st.query_params
    if qp.get("from", "").upper() in hub_codes:
        ss["origin"] = qp["from"].upper()
    if qp.get("to", "").upper() in all_codes:
        ss["dest"] = qp["to"].upper()
    try:
        if float(qp.get("kg", "")) >= 1:
            ss["weight"] = float(qp["kg"])
    except ValueError:
        pass
ss.setdefault("weight", 100.0)
if ss.get("origin") not in hub_codes:
    ss["origin"] = hub_codes[0]
if ss.get("dest") not in all_codes:
    ss["dest"] = next((c for c in ("DEL", "BOM") if c in all_codes and c != ss["origin"]),
                      next((c for c in all_codes if c != ss["origin"]), all_codes[0]))


def swap():
    if ss["dest"] in hub_codes:
        ss["origin"], ss["dest"] = ss["dest"], ss["origin"]
    else:
        ss["_swap_msg"] = f"{ss['dest']} has no departures in the schedule, so it can't be the origin."


label = lambda c: f"{c} – {names[c]}" if names.get(c) else c  # noqa: E731
c1, c2, c3, c4 = st.columns([3, 3, 2, 1], vertical_alignment="bottom")
origin = c1.selectbox("From", hub_codes, key="origin", format_func=label)
dest = c2.selectbox("To", all_codes, key="dest", format_func=label)
weight = c3.number_input("Weight (kg)", min_value=1.0, step=10.0, format="%g", key="weight")
c4.button("⇄", on_click=swap, help="Swap From and To", width="stretch", key="swap")
if ss.get("_swap_msg"):
    st.warning(ss.pop("_swap_msg"))
st.query_params.from_dict({"from": origin, "to": dest, "kg": f"{weight:g}"})  # the address bar is a shareable link

eng, results, status, weekly = queries.price_route(conn, origin, dest, today)
if status == "SAME CITY":
    st.warning("Pick two different cities.")
    st.stop()
if status == "NO SERVICE":
    st.info(f"No nonstop flights from {label(origin)} to {label(dest)}.")
    st.stop()

airline_name = {code: a.get("Name") or code for code, a in eng.airlines.items()}
rows = []
for r in results:
    priced = r.status in (STATUS_OK, STATUS_CHECK) and r.rate_row is not None
    total = r.total_cost(weight) if priced else None
    freight_per_kg = r.freight_charge(weight) / weight if priced else None
    if r.status == STATUS_OK:
        note = "; ".join(f"{s.label} surcharge {s.flag}" for s in r.surcharges if s.flag)
    elif r.status == STATUS_CHECK:
        note = "Check: " + r.match_note
    elif r.status == STATUS_NO_RATE:
        note = "No rate for this route in the tariff"
    else:
        note = "Tariff not loaded"
    rows.append({
        "_ok": r.status == STATUS_OK,
        "Airline": airline_name.get(r.flight["Airline"], r.flight["Airline"]),
        "Flight": r.flight["FlightNo"],
        "Departs": r.flight["DepTime"],
        "Days": "".join(d[0] if r.flight[d] else "·" for d in DAYS),
        "Product": r.product or "",
        "₹/kg all-in": round(total / weight, 2) if total is not None else None,
        "Total ₹": round(total) if total is not None else None,
        "Freight + surch. + add-on (₹/kg)": (f"{freight_per_kg:g} + {r.surcharge_total:g} + {r.addon or 0:g}"
                                            if priced else ""),
        "Note": note,
    })

df = pd.DataFrame(rows)
df["_rank"] = df["₹/kg all-in"].where(df["_ok"])
df = df.sort_values(["_rank", "Departs"], na_position="last").reset_index(drop=True)
best = df.iloc[0] if df["_ok"].any() and pd.notna(df.iloc[0]["_rank"]) else None
ties = int((df["_rank"] == best["_rank"]).sum()) if best is not None else 0

# Headline: the answer first
m1, m2, m3, m4 = st.columns(4)
if best is not None:
    m1.metric("Cheapest flight" if ties == 1 else f"Cheapest (+{ties - 1} at same price)",
              best["Flight"], f"{best['Airline']} · departs {best['Departs']}", delta_color="off", delta_arrow="off",
              border=True)
    m2.metric("All-in per kg", f"₹{best['₹/kg all-in']:g}", border=True)
    m3.metric(f"Total for {weight:g} kg", rs(best["Total ₹"]), border=True)
else:
    m1.metric("Cheapest flight", "–", border=True)
    m2.metric("All-in per kg", "–", border=True)
    m3.metric(f"Total for {weight:g} kg", "–", border=True)
m4.metric("Flights a week", weekly, f"{len(results)} flight numbers", delta_color="off", delta_arrow="off",
          border=True)


cheapest = df["_rank"] == (best["_rank"] if best is not None else None)
shown = df.drop(columns=["_ok", "_rank"])


PRICE_COLS = ["₹/kg all-in", "Total ₹"]


def highlight_best(col):
    return ["background-color: rgba(46, 160, 90, .25); font-weight: 600" if cheapest[i] else "" for i in col.index]


st.dataframe(
    shown.style.apply(highlight_best, subset=PRICE_COLS), hide_index=True, width="stretch",
    height=min(38 + 35 * len(shown), 640),
    column_config={
        "₹/kg all-in": st.column_config.NumberColumn(format="%g"),
        "Total ₹": st.column_config.NumberColumn(format="localized"),
        "Note": st.column_config.TextColumn(width="large"),
    })

files = []
for carrier in ("Air India", "IndiGo"):
    b = eng.batch(carrier)
    if b:
        files.append(f"{carrier} eff. {dmy(b['EffectiveDate'])}")
st.caption(
    f"Cheapest first, green = cheapest. **All-in per kg** = (air freight + surcharges + your add-on) ÷ {weight:g} kg. Freight uses the "
    "minimum charge and the cheaper-higher-weight-break rule, so light shipments cost more per kg. "
    f"Rates: {', '.join(files) or 'none loaded'}.")
