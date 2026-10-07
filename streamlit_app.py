"""
Cargo Tariffs - Streamlit version.

    streamlit run streamlit_app.py

Same engine and same database as the Flask app (instance/tariff.sqlite, or
the TARIFFAPP_DB environment variable), so you can use either front end.
"""

import csv
import datetime as dt
import io
import os
import tempfile

import altair as alt
import pandas as pd
import streamlit as st

from tariffapp import database_path, db, queries
from tariffapp.etl.loader import delete_batch, load_rate_file
from tariffapp.importers import DAYS, import_schedule_workbook
from tariffapp.linker import (GENERAL_PRODUCTS, STATUS_CHECK, STATUS_NO_RATE, STATUS_NO_TARIFF, STATUS_OK)
from tariffapp.schema import CARRIER_TABLES
from tariffapp.settings_store import (APPLIES_TO, save_airlines, save_network_surcharges, save_stations)

st.set_page_config(page_title="Cargo Tariffs", page_icon="✈", layout="wide")

STATUS_LABEL = {STATUS_OK: "✅ OK", STATUS_CHECK: "⚠️ Check", STATUS_NO_RATE: "❌ No rate",
                STATUS_NO_TARIFF: "➖ No tariff"}


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def get_conn():
    """One connection per script run (Streamlit reruns the script on every interaction)."""
    conn = db.connect(database_path())
    if not st.session_state.get("_schema_ok"):
        db.init_schema(conn)
        st.session_state["_schema_ok"] = True
    return conn


def dmy(v):
    if not v:
        return ""
    if isinstance(v, str):
        try:
            v = dt.date.fromisoformat(v[:10])
        except ValueError:
            return v
    return v.strftime("%d-%b-%y")


def days_str(f):
    return "".join(d[0] if f[d] else "·" for d in DAYS)


def station_label(stations):
    names = {s["Code"]: s["Name"] for s in stations}
    return lambda c: f"{c} – {names[c]}" if names.get(c) else c


def as_of_picker(key):
    return st.date_input("Rates as of", value=dt.date.today(), key=key, format="DD/MM/YYYY",
                         help="Uses each airline's rate file that was in force on this date.")


def highlight_min(df, cols, ok_mask):
    """Styler: green background on the cheapest OK value in each column."""
    def style(col):
        vals = col.where(ok_mask)
        best = vals.min()
        return ["background-color: rgba(46, 160, 90, .22); font-weight: 600"
                if pd.notna(v) and pd.notna(best) and v == best and ok else "" for v, ok in zip(col, ok_mask)]
    return df.style.apply(style, subset=cols)


NUM = st.column_config.NumberColumn


def numeric(df, cols):
    """Force rate columns to numbers so blanks show as empty cells, not 'None'."""
    for c in cols:
        if c in df:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    return df


def blank_text(df, cols):
    for c in cols:
        if c in df:
            df[c] = df[c].fillna("")
    return df


# ---------------------------------------------------------------------------
# Route lookup
# ---------------------------------------------------------------------------

def page_route():
    conn = get_conn()
    hubs, stations = queries.stations(conn)
    st.title("✈ Route lookup")
    if not stations:
        st.info("No flight schedule loaded yet. Go to **Upload** and upload the flight-schedule workbook "
                "(the one with the *Raw Data* sheet), then your Air India and IndiGo rate files.")
        return

    hub_codes = [s["Code"] for s in hubs] or [s["Code"] for s in stations]
    all_codes = [s["Code"] for s in stations]
    fmt = station_label(stations)
    ss = st.session_state
    if ss.get("origin") not in hub_codes:
        ss["origin"] = hub_codes[0]
    if ss.get("dest") not in all_codes:
        ss["dest"] = "DEL" if "DEL" in all_codes and ss["origin"] != "DEL" else next(
            (c for c in all_codes if c != ss["origin"]), all_codes[0])

    def swap():
        o, d = ss["origin"], ss["dest"]
        if d in hub_codes:
            ss["origin"], ss["dest"] = d, o
        else:
            ss["_swap_msg"] = f"{d} isn't a hub, so it can't be an origin. Mark it as a hub in Settings → Stations."

    c1, c2, c3, c4, c5 = st.columns([2, 2, 1.4, 1.4, 1])
    origin = c1.selectbox("Origin", hub_codes, key="origin", format_func=fmt)
    dest = c2.selectbox("Destination", all_codes, key="dest", format_func=fmt)
    with c3:
        as_of = as_of_picker("route_date")
    weight = c4.number_input("Shipment weight (kg)", min_value=0.0, step=10.0, value=0.0,
                             help="Optional. Shows the total cost for this chargeable weight.")
    c5.write("")
    c5.button("⇄ Reverse", on_click=swap, width="stretch")
    if ss.get("_swap_msg"):
        st.warning(ss.pop("_swap_msg"))

    eng, results, status, weekly = queries.price_route(conn, origin, dest, as_of)
    names = {s["Code"]: s["Name"] for s in stations}
    m1, m2, m3, m4 = st.columns([3, 1, 1, 1])
    m1.metric("Route", f"{names.get(origin) or origin} → {names.get(dest) or dest}")
    m2.metric("Weekly flights", weekly)
    m3.metric("Flight count", len(results))
    m4.metric("Status", status)

    files = []
    for carrier in CARRIER_TABLES:
        b = eng.batch(carrier)
        files.append(f"{carrier}: **eff. {dmy(b['EffectiveDate'])}** ({b['SourceFileName']})" if b
                     else f"{carrier}: **no rate file for this date**")
    st.caption(" · ".join(files))

    if status == "SAME CITY":
        st.warning("Please select two different cities as Origin and Destination.")
        return
    if status == "NO SERVICE":
        st.info("No nonstop flights currently operate on this route. Try a different Origin / Destination.")
        return

    recs = []
    for r in results:
        r45, b45 = r.rate_at(45)
        r100, b100 = r.rate_at(100)
        notes = [f"{lbl} uses {b}" for lbl, b in (("45+", b45), ("100+", b100)) if r.rate_row and b != lbl]
        recs.append({
            "Airline": r.flight["Airline"], "Flight": r.flight["FlightNo"], "ETD": r.flight["DepTime"],
            "Days": days_str(r.flight), "Product": r.product or "", "Min ₹": r.rates["Min"],
            "Rate 45+": r45, "Rate 100+": r100,
            "Surch. ₹/kg": r.surcharge_total if r.rate_row else None,
            "Add-on": r.addon if r.rate_row else None,
            "CPKG 45+": r.cpkg(45)[0], "CPKG 100+": r.cpkg(100)[0],
            **({f"Total {weight:g} kg ₹": r.total_cost(weight)} if weight else {}),
            "Status": STATUS_LABEL.get(r.status, r.status),
            "Surcharges": r.surcharge_summary, "Break note": ", ".join(notes),
        })
    df = numeric(pd.DataFrame(recs), ["Min ₹", "Rate 45+", "Rate 100+", "Surch. ₹/kg", "Add-on", "CPKG 45+",
                                      "CPKG 100+", f"Total {weight:g} kg ₹"])
    ok = pd.Series([r.status == STATUS_OK for r in results])
    cheap_cols = ["CPKG 45+", "CPKG 100+"] + ([f"Total {weight:g} kg ₹"] if weight else [])
    st.dataframe(
        highlight_min(df, cheap_cols, ok), hide_index=True, width="stretch",
        column_config={
            "Min ₹": NUM(format="%d"), "Rate 45+": NUM(format="%g"), "Rate 100+": NUM(format="%g"),
            "Surch. ₹/kg": NUM(format="%g"), "Add-on": NUM(format="%g"),
            "CPKG 45+": NUM(format="%g"), "CPKG 100+": NUM(format="%g"),
            f"Total {weight:g} kg ₹": NUM(format="localized"),
        })
    st.caption("CPKG (₹/kg) = tariff rate + surcharges + add-on (X-ray / TSP / handling). Green = cheapest. "
               "*Break note* means the tariff has no rate at that weight break, so the next lower break is used."
               + (" Total = freight (with minimum charge and the higher-break rule) + (surcharges + add-on) × weight."
                  if weight else ""))

    with st.expander("Flight details – how the rate was picked"):
        pick = st.selectbox("Flight", range(len(results)),
                            format_func=lambda i: f"{results[i].flight['FlightNo']} · {results[i].flight['DepTime']}")
        r = results[pick]
        st.markdown(f"**{STATUS_LABEL.get(r.status)}** – {r.match_note}")
        if r.condition and r.condition.text:
            st.markdown(f"Tariff condition: *{r.condition.text}*")
        if r.rate_row:
            st.markdown("Rates — " + " · ".join(f"{k}: {v:g}" for k, v in r.rates.items() if v is not None))
            if r.surcharges:
                st.markdown("Surcharges:\n" + "\n".join(
                    f"- **{s.short}** – {s.reason}" for s in r.surcharges))
            st.markdown(f"Add-on: ₹{r.addon or 0:g}/kg {f'({r.addon_note})' if r.addon_note else ''}  \n"
                        f"Rate file: {r.source_file} (eff. {dmy(r.effective_date)})")


# ---------------------------------------------------------------------------
# All flights
# ---------------------------------------------------------------------------

def page_flights():
    conn = get_conn()
    st.title("All flights & their linked rates")
    hubs, stations = queries.stations(conn)
    airlines = [r[0] for r in conn.execute("SELECT DISTINCT Airline FROM Flights ORDER BY 1")]
    c = st.columns([1, 1, 1, 1.3, 1, 1.3])
    fo = c[0].multiselect("Origin", [s["Code"] for s in hubs])
    fd = c[1].multiselect("Destination", [s["Code"] for s in stations])
    fa = c[2].multiselect("Airline", airlines)
    fs = c[3].multiselect("Status", list(STATUS_LABEL), format_func=STATUS_LABEL.get)
    q = c[4].text_input("Flight no.", placeholder="6E449").strip().upper()
    with c[5]:
        as_of = as_of_picker("flights_date")

    results = queries.all_priced(conn, as_of)
    shown = [r for r in results
             if (not fo or r.flight["Origin"] in fo) and (not fd or r.flight["Destination"] in fd)
             and (not fa or r.flight["Airline"] in fa) and (not fs or r.status in fs)
             and (not q or q in r.flight["FlightNo"].upper())]
    counts = pd.Series([r.status for r in results]).value_counts()
    st.caption(f"{len(shown)} of {len(results)} flights shown · "
               + " · ".join(f"{STATUS_LABEL.get(k, k)}: {v}" for k, v in counts.items()))

    df = pd.DataFrame([{
        "Route": f"{r.flight['Origin']}–{r.flight['Destination']}", "Airline": r.flight["Airline"],
        "Flight": r.flight["FlightNo"], "ETD": r.flight["DepTime"], "Days": days_str(r.flight),
        "Product": r.product or "", "Min": r.rates["Min"], "N": r.rates["N"], "45+": r.rates["45"],
        "100+": r.rates["100"], "300+": r.rates["300"], "500+": r.rates["500"], "1000+": r.rates["1000"],
        "Surcharges": r.surcharge_summary, "CPKG 45+": r.cpkg(45)[0],
        "Status": STATUS_LABEL.get(r.status, r.status), "How matched": r.match_note,
        "Tariff condition": r.condition.text if r.condition else "",
    } for r in shown])
    df = numeric(df, ["Min", "N", "45+", "100+", "300+", "500+", "1000+", "CPKG 45+"])
    st.dataframe(df, hide_index=True, width="stretch", height=600,
                 column_config={k: NUM(format="%g") for k in ("Min", "N", "45+", "100+", "300+", "500+", "1000+",
                                                               "CPKG 45+")})

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(queries.FLIGHT_EXPORT_HEADER)
    for r in results:
        w.writerow(queries.flight_export_row(r))
    st.download_button("⬇ Download all flights (CSV)", buf.getvalue(), file_name=f"flight_rates_{as_of}.csv",
                       mime="text/csv")


# ---------------------------------------------------------------------------
# Rate history
# ---------------------------------------------------------------------------

def page_history():
    conn = get_conn()
    st.title("Rate history")
    _, stations = queries.stations(conn)
    codes = [s["Code"] for s in stations]
    c = st.columns(4)
    carrier = c[0].selectbox("Carrier", list(CARRIER_TABLES))
    products = [p for p in CARRIER_TABLES[carrier] if p != "Other"]
    product = c[1].selectbox("Product", products, index=products.index(GENERAL_PRODUCTS[carrier][0]))
    origin = c[2].text_input("Origin", value="DEL").strip().upper()
    dest = c[3].text_input("Destination", value="BOM").strip().upper()
    if codes and (origin not in codes or dest not in codes):
        st.caption("Tip: any airport code in the rate files works, not just the stations in your schedule.")
    if not origin or not dest:
        return

    rows = queries.lane_history(conn, carrier, product, origin, dest)
    if not rows:
        st.info(f"No {carrier} {product} rates found for {origin} → {dest} in any loaded file.")
        return

    metric = st.radio("Weight break", ["N", "Q45", "Q100", "Q300"], index=1, horizontal=True,
                      format_func={"N": "N rate", "Q45": "45+", "Q100": "100+", "Q300": "300+"}.get)
    data = pd.DataFrame([{"Effective": pd.to_datetime(r["EffectiveDate"][:10]),
                          "Condition": queries.history_label(r), "Rate": r[f"Rate{metric}"]} for r in rows])
    data = data.dropna(subset=["Rate"])
    if data.empty:
        st.info("No rates at this weight break – try another one.")
    else:
        chart = (alt.Chart(data).mark_line(interpolate="step-after", point=True)
                 .encode(x=alt.X("Effective:T", title="Effective date"),
                         y=alt.Y("Rate:Q", title="₹ / kg", axis=alt.Axis(format="~g", tickMinStep=1),
                                 scale=alt.Scale(zero=False, padding=20)),
                         color=alt.Color("Condition:N", legend=alt.Legend(orient="bottom", labelLimit=500)),
                         tooltip=["Effective:T", "Condition:N", "Rate:Q"])
                 .properties(height=360))
        st.altair_chart(chart, width="stretch")

    st.dataframe(numeric(pd.DataFrame([{
        "Effective": dmy(r["EffectiveDate"]), "Condition": queries.history_label(r), "Min": r["MinCharge"],
        "N": r["RateN"], "45+": r["RateQ45"], "100+": r["RateQ100"], "300+": r["RateQ300"],
        "500+": r["RateQ500"], "1000+": r["RateQ1000"], "File": r["SourceFileName"],
    } for r in reversed(rows)]), ["Min", "N", "45+", "100+", "300+", "500+", "1000+"]), hide_index=True, width="stretch")


# ---------------------------------------------------------------------------
# Tariff browser
# ---------------------------------------------------------------------------

def page_tariffs():
    conn = get_conn()
    st.title("Tariff browser")
    batches = [dict(r) for r in conn.execute(
        "SELECT * FROM LoadBatches WHERE Status='OK' ORDER BY EffectiveDate DESC, BatchID DESC")]
    if not batches:
        st.info("No rate files loaded yet – upload some on the **Upload** page.")
        return
    c = st.columns([3, 1, 1])
    batch = c[0].selectbox("Rate file", batches,
                           format_func=lambda b: f"{b['Carrier']} · eff. {dmy(b['EffectiveDate'])} · "
                                                 f"{b['SourceFileName']}")
    origin = c[1].text_input("Origin", placeholder="any").strip().upper()
    dest = c[2].text_input("Destination", placeholder="any").strip().upper()
    counts = queries.batch_product_counts(conn, batch)
    default = next((p for p in counts if counts[p]), next(iter(counts)))
    product = st.radio("Product", list(counts), index=list(counts).index(default), horizontal=True,
                       format_func=lambda p: f"{p} ({counts[p]:,})")
    limit = 5000
    rows = queries.tariff_rows(conn, batch, product, origin or None, dest or None, limit=limit + 1)
    if len(rows) > limit:
        st.caption(f"Showing the first {limit:,} rows – filter by origin / destination to narrow it down.")
        rows = rows[:limit]
    carrier = batch["Carrier"]
    cols = (["Region", "OriginCode", "DestCode", "RateCategory", queries.CONDITION_COL[carrier], "MinCharge", "RateN",
             "RateQ45", "RateQ100", "RateQ300", "RateQ500", "RateQ1000"] + queries.EXTRA_COLS[carrier])
    df = numeric(pd.DataFrame(rows, columns=cols),
                 ["MinCharge", "RateN", "RateQ45", "RateQ100", "RateQ300", "RateQ500", "RateQ1000"])
    st.dataframe(blank_text(df, [c for c in cols if df[c].dtype == object]), hide_index=True, width="stretch", height=600)


# ---------------------------------------------------------------------------
# Notes
# ---------------------------------------------------------------------------

def page_notes():
    conn = get_conn()
    st.title("Tariff notes & footnotes")
    st.caption("From the latest rate file of each carrier. Network-wide surcharges mentioned here (fuel, SS, "
               "ex-BOM…) go into **Settings → Network surcharges** so they're added to every matching flight.")
    n = queries.notes(conn)
    left, right = st.columns(2)
    with left:
        st.subheader("Air India – Notation")
        if not n["ai"]:
            st.info("No Air India file loaded.")
        else:
            st.caption(f"{n['ai']['SourceFileName']} · eff. {dmy(n['ai']['EffectiveDate'])}")
            for x in n["ai_notes"]:
                if x["HeadingText"]:
                    st.markdown(f"**{x['SectionNo']}. {x['HeadingText']}**")
                elif x["DetailText"]:
                    st.markdown(x["DetailText"].replace("\n", "  \n"))
    with right:
        st.subheader("IndiGo – Footnotes")
        if not n["ig"]:
            st.info("No IndiGo file loaded.")
        else:
            st.caption(f"{n['ig']['SourceFileName']} · eff. {dmy(n['ig']['EffectiveDate'])}")
            for x in n["ig_notes"]:
                st.markdown(f"{x['SeqNo'] or '•'}. {x['FootnoteText']}")
    if n["tsp"]:
        st.subheader("IndiGo TSP / terminal charges")
        st.dataframe(pd.DataFrame(n["tsp"]).drop(columns=["TSPID", "BatchID", "EffectiveDate"]),
                     hide_index=True, width="stretch")


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

def page_exceptions():
    conn = get_conn()
    st.title("Exceptions")
    as_of = as_of_picker("exc_date")
    ex = queries.exceptions(conn, as_of)

    st.subheader(f"Flights that need a look ({len(ex['check'])})")
    if ex["check"]:
        st.dataframe(pd.DataFrame([{
            "Route": f"{r.flight['Origin']}–{r.flight['Destination']}", "Airline": r.flight["Airline"],
            "Flight": r.flight["FlightNo"], "ETD": r.flight["DepTime"], "Status": STATUS_LABEL.get(r.status),
            "Note": r.match_note, "Tariff condition": r.condition.text if r.condition else "",
        } for r in ex["check"]]), hide_index=True, width="stretch")
    else:
        st.success("None – every flight with a loaded tariff found its rate.")

    st.subheader(f"Surcharges past their end date ({len(ex['ended'])} flights)")
    if ex["ended"]:
        st.caption("Still in the latest rate file but its remarks say the validity has ended. They're still added "
                   "to CPKG – confirm with the airline.")
        st.dataframe(pd.DataFrame([{"Route": f"{r.flight['Origin']}–{r.flight['Destination']}",
                                    "Flight": r.flight["FlightNo"], "Surcharges": r.surcharge_summary}
                                   for r in ex["ended"]]), hide_index=True, width="stretch")
    else:
        st.success("None.")

    st.subheader("Airlines without a tariff")
    if ex["no_tariff"]:
        st.markdown("\n".join(f"- **{a}** – {n} flights" for a, n in ex["no_tariff"].most_common()))
    else:
        st.success("None.")

    st.subheader(f"Unclassified rate rows in the latest files ({len(ex['other'])})")
    if ex["other"]:
        st.caption("Rows the loader couldn't place in a product table – a new product name usually shows up here.")
        st.dataframe(pd.DataFrame(ex["other"])[["Carrier", "OriginCode", "DestCode", "RateCategory", "SourceSheet",
                                                "RateN"]], hide_index=True, width="stretch")
    else:
        st.success("None.")


# ---------------------------------------------------------------------------
# Upload
# ---------------------------------------------------------------------------

def _save_upload(f):
    folder = os.path.join(os.path.dirname(database_path()), "uploads")
    os.makedirs(folder, exist_ok=True)
    fd, path = tempfile.mkstemp(suffix=".xlsx", dir=folder,
                                prefix=dt.datetime.now().strftime("%Y%m%d-%H%M%S_"))
    with os.fdopen(fd, "wb") as out:
        out.write(f.getbuffer())
    return path


def page_upload():
    conn = get_conn()
    st.title("Upload files")
    left, right = st.columns(2)
    with left, st.form("rates", clear_on_submit=True):
        st.subheader("Rate files (Air India / IndiGo)")
        st.caption("Upload the new rate list(s) as you receive them. Carrier and effective date are read from the "
                   "file and its name (e.g. *8.1 6E Eff. 16th Aug 2026.xlsx*). Files already loaded are skipped.")
        files = st.file_uploader("Rate files", type=["xlsx"], accept_multiple_files=True,
                                 label_visibility="collapsed")
        with st.expander("Options"):
            use_date = st.checkbox("Set the effective date myself (only if the filename has none)")
            eff = st.date_input("Effective date", value=dt.date.today(), format="DD/MM/YYYY")
            force = st.checkbox("Load again even if already loaded")
        go_rates = st.form_submit_button("Upload rate files", type="primary")
    with right, st.form("schedule", clear_on_submit=True):
        st.subheader("Flight schedule workbook")
        st.caption("*Raw Data* replaces the current flight schedule. *Lists*, *Add-on Rates* and *Network "
                   "Surcharges* are imported too if present (surcharges replace the ones in Settings).")
        sched = st.file_uploader("Schedule", type=["xlsx", "xlsm"], label_visibility="collapsed")
        go_sched = st.form_submit_button("Upload schedule", type="primary")

    if go_rates:
        if not files:
            st.warning("Choose at least one .xlsx file.")
        for f in files or []:
            with st.spinner(f"Loading {f.name}…"):
                r = load_rate_file(conn, _save_upload(f), filename=f.name, force=force,
                                   effective_date=eff if use_date else None)
            eff_s = dmy(r["effective_date"]) if r["effective_date"] else "unknown date"
            msg = f"**{f.name}**: {r['carrier'] or ''} {r['message']} (effective {eff_s})"
            {"ok": st.success, "skipped": st.info}.get(r["status"], st.error)(msg)
            for w in r["warnings"]:
                st.warning(f"{f.name}: {w}")
    if go_sched:
        if not sched:
            st.warning("Choose the schedule workbook.")
        else:
            try:
                s = import_schedule_workbook(conn, _save_upload(sched), filename=sched.name)
                st.success(f"**{sched.name}**: imported {s['flights']} flights, {s['stations']} stations, "
                           f"{s['airlines']} add-on rates, {s['surcharges']} network surcharges.")
            except Exception as e:  # noqa: BLE001 - show the problem to the user
                conn.rollback()
                st.error(f"{sched.name}: {e}")

    last = conn.execute("SELECT * FROM ScheduleImports ORDER BY ImportID DESC LIMIT 1").fetchone()
    if last:
        st.caption(f"Current schedule: {last['SourceFileName']} – {last['FlightCount']} flights, "
                   f"imported {last['ImportedAt']}")

    batches = [dict(r) for r in conn.execute("SELECT * FROM LoadBatches ORDER BY BatchID DESC")]
    st.subheader(f"Loaded rate files ({len(batches)})")
    if not batches:
        st.caption("Nothing loaded yet.")
        return
    st.dataframe(pd.DataFrame([{
        "#": b["BatchID"], "Carrier": b["Carrier"], "File": b["SourceFileName"], "Effective": dmy(b["EffectiveDate"]),
        "Loaded": b["LoadedAt"], "Rows": b["RowsLoaded"], "Status": b["Status"], "Notes": b["Notes"] or "",
    } for b in batches]), hide_index=True, width="stretch")

    with st.expander("Delete a loaded file"):
        b = st.selectbox("File", batches, format_func=lambda b: f"#{b['BatchID']} · {b['Carrier']} · "
                                                                  f"{b['SourceFileName']}")
        sure = st.checkbox("Yes, delete this file and all its rates")
        if st.button("Delete", disabled=not sure):
            delete_batch(conn, b["BatchID"])
            st.success(f"Deleted {b['SourceFileName']}.")
            st.rerun()


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

def _save(fn, conn, edited, what):
    try:
        n = fn(conn, edited.to_dict("records"))
        st.success(f"Saved {n} {what}.")
    except ValueError as e:
        st.error(str(e))


def _dates(series):
    return pd.to_datetime(series, errors="coerce").dt.date


def page_settings():
    conn = get_conn()
    st.title("Settings")
    t1, t2, t3 = st.tabs(["Network surcharges", "Airlines & add-on costs", "Stations"])

    with t1:
        st.caption("Surcharges from the tariff notes / footnotes that apply across many routes. Route- and "
                   "flight-specific surcharges are read from the rate files automatically – don't list them here. "
                   "**Applies to**: All, Origin, Destination or Route (station `BOM`, route `DEL-COK`). Leave "
                   "**Valid to** blank for \"until further notice\". Add rows at the bottom; select rows and press "
                   "Delete to remove them. Then **Save**.")
        df = pd.read_sql_query("SELECT Carrier, AppliesTo, Station, RatePerKg, ValidFrom, ValidTo, Label, Reason, "
                               "Active FROM NetworkSurcharges ORDER BY Carrier, AppliesTo, Station, SurchargeID",
                               conn)
        df["ValidFrom"], df["ValidTo"] = _dates(df["ValidFrom"]), _dates(df["ValidTo"])
        df["Active"] = df["Active"].astype(bool)
        blank_text(df, ["Station", "Label", "Reason"])
        edited = st.data_editor(df, num_rows="dynamic", hide_index=True, width="stretch",
                                key="ed_surch", column_config={
                                    "Carrier": st.column_config.SelectboxColumn(options=list(CARRIER_TABLES),
                                                                                required=True),
                                    "AppliesTo": st.column_config.SelectboxColumn("Applies to", options=APPLIES_TO,
                                                                                  required=True, default="All"),
                                    "Station": st.column_config.TextColumn("Station / route"),
                                    "RatePerKg": NUM("₹/kg", min_value=0, format="%g", required=True),
                                    "ValidFrom": st.column_config.DateColumn("Valid from", format="DD-MMM-YY"),
                                    "ValidTo": st.column_config.DateColumn("Valid to", format="DD-MMM-YY"),
                                    "Active": st.column_config.CheckboxColumn(default=True),
                                    "Reason": st.column_config.TextColumn("Reason / source", width="large"),
                                })
        if st.button("Save surcharges", type="primary"):
            _save(save_network_surcharges, conn, edited, "surcharges")

    with t2:
        st.caption("**Tariff** links an airline code from the schedule to its loaded rate files. **Add-on** is "
                   "your own per-kg cost on top of the tariff (X-ray, TSP, handling…) and is included in CPKG.")
        df = pd.read_sql_query("SELECT Code, Name, TariffCarrier, AddOnPerKg, AddOnIncludes FROM Airlines "
                               "ORDER BY Code", conn)
        blank_text(df, ["Name", "AddOnIncludes"])
        edited = st.data_editor(df, num_rows="dynamic", hide_index=True, width="stretch",
                                key="ed_air", column_config={
                                    "TariffCarrier": st.column_config.SelectboxColumn(
                                        "Tariff", options=list(CARRIER_TABLES),
                                        help="Blank = no tariff loaded for this airline"),
                                    "AddOnPerKg": NUM("Add-on ₹/kg", min_value=0, format="%g"),
                                    "AddOnIncludes": st.column_config.TextColumn("Includes", width="large"),
                                })
        if st.button("Save airlines", type="primary"):
            _save(save_airlines, conn, edited, "airlines")

    with t3:
        st.caption("**Hub** stations appear in the Origin list. **Tariff code** is only needed when the rate files "
                   "use a different code from the schedule (e.g. schedule `TVM`, tariffs `TRV`).")
        df = pd.read_sql_query("SELECT Code, Name, IsHub, TariffCode, SortOrder FROM Stations "
                               "ORDER BY IsHub DESC, COALESCE(SortOrder, 999), Code", conn)
        df["IsHub"] = df["IsHub"].astype(bool)
        blank_text(df, ["Name", "TariffCode"])
        edited = st.data_editor(df, num_rows="dynamic", hide_index=True, width="stretch",
                                key="ed_st", column_config={
                                    "IsHub": st.column_config.CheckboxColumn("Hub", default=False),
                                    "TariffCode": st.column_config.TextColumn("Tariff code"),
                                    "SortOrder": NUM("Sort order", format="%d"),
                                })
        if st.button("Save stations", type="primary"):
            _save(save_stations, conn, edited, "stations")


# ---------------------------------------------------------------------------

pages = [
    st.Page(page_route, title="Route lookup", icon="✈️", default=True),
    st.Page(page_flights, title="All flights", icon="📋", url_path="flights"),
    st.Page(page_history, title="Rate history", icon="📈", url_path="history"),
    st.Page(page_tariffs, title="Tariffs", icon="🗂️", url_path="tariffs"),
    st.Page(page_notes, title="Notes", icon="📝", url_path="notes"),
    st.Page(page_exceptions, title="Exceptions", icon="⚠️", url_path="exceptions"),
    st.Page(page_upload, title="Upload", icon="⬆️", url_path="upload"),
    st.Page(page_settings, title="Settings", icon="⚙️", url_path="settings"),
]
if __name__ == "__main__":  # `streamlit run` executes this file as __main__; tests import the page functions
    st.navigation(pages).run()
