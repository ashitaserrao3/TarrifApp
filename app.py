import copy
import datetime as dt
import hashlib
import html
import os
import re
import tempfile

import streamlit as st
import streamlit_authenticator as stauth

import allcargo
import supabase_store
import tariff_changes
from tariffapp import database_path, db, queries, sync
from tariffapp.linker import STATUS_CHECK, STATUS_NO_RATE, STATUS_NO_TARIFF, TariffEngine

st.set_page_config(page_title="Flight Schedule", page_icon="✈️", layout="wide")

# =====================================================
# AUTHENTICATION
# Users + cookie key live in Streamlit Secrets, never in the repo.
#   Community Cloud : App settings -> Secrets
#   Local run       : .streamlit/secrets.toml (git-ignored)
# =====================================================


def _to_dict(obj):
    """st.secrets is read-only; the authenticator needs a normal dict."""
    if hasattr(obj, "items"):
        return {k: _to_dict(v) for k, v in obj.items()}
    return copy.deepcopy(obj)


if "credentials" not in st.secrets or "cookie" not in st.secrets:
    st.error(
        "Login settings are missing. Add the [credentials] and [cookie] sections "
        "to Streamlit Secrets (see secrets.toml.example)."
    )
    st.stop()

credentials = _to_dict(st.secrets["credentials"])
cookie = _to_dict(st.secrets["cookie"])

authenticator = stauth.Authenticate(
    credentials,
    cookie["name"],
    cookie["key"],
    float(cookie.get("expiry_days", 30)),
    auto_hash=False,  # passwords in secrets are already bcrypt hashes
)

# =====================================================
# LOGIN SCREEN
# =====================================================

if not st.session_state.get("authentication_status"):
    _, mid, _ = st.columns([1, 1.1, 1])
    with mid:
        st.html("""<div style="text-align:center;margin:8vh 0 18px;font-family:Inter,system-ui,sans-serif">
<div style="font-size:40px">✈</div>
<div style="font-size:24px;font-weight:700;color:#1F4E78;letter-spacing:.06em">FLIGHT SCHEDULE</div>
<div style="color:#64748B;font-size:14px;margin-top:4px">Sign in to see today's air cargo rates</div></div>""")
        try:
            authenticator.login(
                fields={"Form name": "Sign in", "Username": "Username",
                        "Password": "Password", "Login": "Sign in"},
            )
        except Exception as e:
            st.error(e)
        if st.session_state.get("authentication_status") is False:
            st.error("Username or password is incorrect", icon="🔒")

    if st.session_state.get("authentication_status"):
        st.rerun()  # signed in (or remembered) -> show the flight schedule
    st.stop()

# =====================================================
# SIGNED IN: FLIGHT SCHEDULE
# Everyone who signs in sees the schedule, built from the files in data/
# (see README). New airline tariffs Ashita publishes are kept in Supabase Storage.
# =====================================================

# Colours: the workbook's navy / green, softened for the web
NAVY, NAVY_DARK, BLUE, GREEN_DARK = "#1F4E78", "#16324F", "#2E74B5", "#2F5130"
INK, MUTED, LINE, STRIPE, HOVER = "#0F1B33", "#64748B", "#E5E9F0", "#F8FAFC", "#EEF4FB"
STATUS_FILL = {"ACTIVE": ("#DCFCE7", "#166534"), "NO SERVICE": ("#FEE2E2", "#991B1B"),
               "SAME CITY": ("#FEF3C7", "#92400E")}
CHECK_AMBER = "#FFE699"
LOWEST_RED = "#FFC7CE"  # Excel's "light red fill"
AIRLINE_DOT = {"INDIGO": "#1D4ED8", "AIRINDIA": "#B91C1C", "AKASA": "#F97316", "SPICEJET": "#EAB308",
               "STARAIR": "#0EA5E9", "ALLIANCEAIR": "#64748B"}

SHEET_CSS = f"""
<style>
.xl {{ font-family: Inter, system-ui, -apple-system, "Segoe UI", Arial, sans-serif; color: {INK}; }}
.xl-title {{ background: linear-gradient(120deg, {NAVY_DARK}, {NAVY} 60%, {BLUE}); color: #fff; border-radius: 14px;
             padding: 16px 22px; display: flex; align-items: center; justify-content: space-between; gap: 12px;
             box-shadow: 0 6px 18px rgba(22, 50, 79, .18); }}
.xl-title .t {{ font-weight: 700; font-size: 22px; letter-spacing: .12em; }}
.xl-title .s {{ font-size: 13px; opacity: .8; letter-spacing: .02em; }}
.xl-band {{ display: grid; grid-template-columns: 2.2fr 1fr 1fr 1.2fr; gap: 12px; }}
.xl-band > div {{ background: #fff; border: 1px solid {LINE}; border-radius: 12px; padding: 12px 16px; min-width: 0;
                  box-shadow: 0 1px 2px rgba(15, 27, 51, .04); }}
.xl-band .k {{ font-size: 11px; font-weight: 600; letter-spacing: .08em; color: {MUTED}; text-transform: uppercase; }}
.xl-band .v {{ color: {NAVY}; font-weight: 700; font-size: 26px; line-height: 1.3; font-variant-numeric: tabular-nums; }}
.xl-band .route {{ font-size: 18px; overflow-wrap: anywhere; }}
.xl-band .route span {{ color: {MUTED}; font-weight: 500; }}
.xl-band .st {{ display: inline-block; font-size: 14px; letter-spacing: .06em; border-radius: 999px; padding: 4px 12px;
                margin-top: 4px; }}
.xl-msg {{ background: #FFF7ED; border: 1px solid #FED7AA; color: #9A3412; border-radius: 10px; font-weight: 600;
           text-align: center; padding: 10px 14px; margin-top: 12px; font-size: 14px; }}
.xl-special {{ background: #F5F0FC; border: 1px solid #E4D7F5; color: #5B2C83; border-radius: 10px; font-weight: 600;
               text-align: center; padding: 8px 14px; margin-top: 12px; font-size: 13px; }}
.xl-wrap {{ overflow-x: auto; margin-top: 14px; background: #fff; border: 1px solid {LINE}; border-radius: 12px;
            box-shadow: 0 1px 3px rgba(15, 27, 51, .06); }}
.xl-table {{ border-collapse: separate; border-spacing: 0; width: 100%; font-size: 14px; }}
.xl-table th {{ background: {NAVY}; color: #fff; font-weight: 600; font-size: 12px; letter-spacing: .06em;
                text-align: center; padding: 10px 12px; white-space: nowrap; border-bottom: 1px solid rgba(255,255,255,.15); }}
.xl-table th.cpkg {{ background: {GREEN_DARK}; }}
.xl-table th.l {{ text-align: left; }}
.xl-table td {{ padding: 10px 12px; border-bottom: 1px solid {LINE}; white-space: nowrap; }}
.xl-table tbody tr:last-child td {{ border-bottom: none; }}
.xl-table tbody tr:nth-child(even) td {{ background: {STRIPE}; }}
.xl-table tbody tr:hover td {{ background: {HOVER}; }}
.xl-table td.c {{ text-align: center; }}
.xl-table td.air {{ font-weight: 600; }}
.xl-table .dot {{ display: inline-block; width: 9px; height: 9px; border-radius: 50%; margin-right: 8px; }}
.xl-table td.fno {{ font-variant-numeric: tabular-nums; color: {NAVY}; font-weight: 500; }}
.xl-table td.cpkg {{ text-align: center; color: {GREEN_DARK}; font-weight: 700; font-size: 15px;
                     font-variant-numeric: tabular-nums; }}
.xl-table td.cpkg.lowest {{ background: {LOWEST_RED} !important; color: #9B1C31; }}
.xl-table td.check {{ background: {CHECK_AMBER} !important; }}
.xl-table td.muted {{ color: #94A3B8; }}
.xl-table sup {{ color: #9C5700; font-size: 10px; font-weight: 400; }}
.xl-legend {{ display: flex; flex-wrap: wrap; gap: 14px; align-items: center; color: {MUTED}; font-size: 12px;
              margin-top: 12px; }}
.xl-legend i {{ display: inline-block; width: 14px; height: 14px; border-radius: 4px; vertical-align: -2px;
                margin-right: 6px; }}
@media (max-width: 700px) {{
  .xl-band {{ grid-template-columns: 1fr 1fr; gap: 8px; }}
  .xl-band > div:first-child {{ grid-column: 1 / -1; }}
  .xl-band .v {{ font-size: 22px; }}
  .xl-title {{ padding: 12px 16px; }}
  .xl-title .t {{ font-size: 18px; }}
  .xl-title .s {{ display: none; }}
}}
.sp {{ background: #E4D7F5; color: #5B2C83; font-size: 10px; font-weight: 700; padding: 2px 6px; border-radius: 999px;
       letter-spacing: .04em; margin-left: 4px; }}
</style>
"""

WIDGET_CSS = f"""
<style>
.block-container {{ padding-top: 1.6rem; max-width: 1280px; }}
/* From / To pickers */
[class*="st-key-origin"] div[data-baseweb="select"] > div, [class*="st-key-dest"] div[data-baseweb="select"] > div {{
  background: #fff; border-radius: 10px; min-height: 44px;
}}
[class*="st-key-origin"] input, [class*="st-key-dest"] input {{ color: {NAVY}; font-weight: 700; font-size: 17px; }}
[class*="st-key-origin"] label p, [class*="st-key-dest"] label p {{ font-size: 12px; font-weight: 600; letter-spacing: .08em;
  color: {MUTED}; text-transform: uppercase; }}
.st-key-swap button {{ border-radius: 999px; min-height: 44px; font-size: 18px; }}
.xl-name {{ color: {MUTED}; font-size: 13px; margin-top: -6px; }}
</style>
"""

TITLE = ('<div class="xl xl-title"><div class="t">✈&nbsp; FLIGHT SCHEDULE</div>'
         '<div class="s">Air cargo CPKG by route · ₹ per kg</div></div>')


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


@st.cache_resource(show_spinner="Loading the latest data…", max_entries=1)
def build_database(signature):
    """A fresh database from exactly the files in the data folder, shared by every visitor and rebuilt
    whenever the files change - so the schedule always shows exactly the files in data/."""
    name = hashlib.sha1(repr(signature).encode()).hexdigest()[:12]
    path = os.path.join(os.path.dirname(database_path()), f"tariff-{name}.sqlite")
    tmp = path + ".tmp"
    for p in (tmp, tmp + "-wal", tmp + "-shm"):
        if os.path.exists(p):
            os.remove(p)
    c = db.connect(tmp)
    try:
        db.init_schema(c)
        result = sync.sync_data_folder(c)
        c.execute("PRAGMA journal_mode = DELETE")  # one self-contained file, safe to rename
    finally:
        c.close()
    os.replace(tmp, path)
    folder = os.path.dirname(path)
    for f in os.listdir(folder):  # databases built from older files
        if f.startswith("tariff-") and not f.startswith(os.path.basename(path)):
            os.remove(os.path.join(folder, f))
    return path, result


def use_rates(files):
    """Make data/rates hold exactly these files {'rates/<name>': bytes}."""
    folder = os.path.join(sync.data_dir(), "rates")
    os.makedirs(folder, exist_ok=True)
    names = {k.split("/", 1)[1] for k in files}
    for n in os.listdir(folder):
        if n not in names:
            os.remove(os.path.join(folder, n))
    for key, data in files.items():
        path = os.path.join(folder, key.split("/", 1)[1])
        if not os.path.exists(path) or open(path, "rb").read() != data:
            with open(path, "wb") as f:
                f.write(data)


def cloud():
    """The Supabase bucket published tariffs are kept in, or None while Secrets don't set it up."""
    cfg = st.secrets.get("supabase", {})
    if not cfg.get("url") or not cfg.get("key"):
        return None
    return supabase_store.SupabaseStore(cfg["url"], cfg["key"], cfg.get("bucket", "tariffs"))


@st.cache_resource(show_spinner="Loading the latest published tariffs…")
def restore_published():
    """Once per app start: bring back the tariffs last published to Supabase. -> error message or None"""
    store = cloud()
    if store is None:
        return None
    try:
        files = {k: v for k, v in store.download().items() if k.startswith("rates/") and k.count("/") == 1}
    except Exception as e:  # noqa: BLE001 - the app still runs on the tariffs in the repo
        return f"Couldn't load the published tariffs from Supabase: {e}"
    if files:
        use_rates(files)
    return None


def data_signature():
    """Changes whenever a file in the data folder is added, replaced or removed."""
    paths = [os.path.join(root, n) for root, _, names in os.walk(sync.data_dir()) for n in names]
    return tuple(sorted((p, os.path.getsize(p), int(os.path.getmtime(p))) for p in paths))


def newest(folder, extensions):
    paths = [os.path.join(folder, f) for f in os.listdir(folder)
             if f.lower().endswith(extensions)] if os.path.isdir(folder) else []
    return max(paths, key=lambda p: (os.path.getmtime(p), p), default=None)


@st.cache_resource(show_spinner="Loading the Allcargo, OCDC and special rates…", max_entries=1)
def load_extras(signature):
    """(Allcargo quotation, Akasa special rates, Akasa OCDC station charges, errors) from the data folder."""
    quote = special = None
    ocdc, errors = {}, []
    for sub, ext, reader in (("allcargo", sync.XLSX, allcargo.read_quote), ("special", (".pdf",), allcargo.read_special)):
        path = newest(os.path.join(sync.data_dir(), sub), ext)
        if not path:
            continue
        try:
            data = reader(path)
            data["file"] = os.path.basename(path)
            if sub == "allcargo":
                quote = data
            else:
                special = data
        except Exception as e:  # noqa: BLE001 - shown on the page
            errors.append(f"{os.path.basename(path)}: {e}")
    rates = os.path.join(sync.data_dir(), "rates")
    for name in sorted(os.listdir(rates)) if os.path.isdir(rates) else []:
        if name.lower().endswith(sync.XLSX) and not ocdc:
            try:
                ocdc = allcargo.read_ocdc(os.path.join(rates, name))  # only the Akasa tariff has an OCDC sheet
            except Exception as e:  # noqa: BLE001 - shown on the page
                errors.append(f"{name}: {e}")
    return quote, special, ocdc, errors


ss = st.session_state
today = dt.date.today()

restore_error = restore_published()
signature = data_signature()
db_path, sync_result = build_database(signature)
quote, special, ocdc, extras_errors = load_extras(signature)
conn = db.connect(db_path)
sched_info = queries.schedule_info(conn, today)

for e in sync_result["errors"] + extras_errors:
    st.error(f"Couldn't read {e}")

top = st.columns([6, 2, 1], vertical_alignment="center")
top[1].caption(f"Signed in as **{esc(ss.get('name'))}**")
with top[2]:
    authenticator.logout("Sign out", use_container_width=True)
if not ss.get("authentication_status"):
    st.rerun()  # just signed out -> back to the login screen

# =====================================================
# UPDATE TARIFF (only ADMIN_USER): upload a new airline tariff, review what changed, publish it
# =====================================================

ADMIN_USER = "ashita"


@st.cache_resource(show_spinner="Reading the tariffs in use…", max_entries=1)
def tariffs_in_use(signature):
    """{carrier: [(file name, effective date, {line: row})]} for the airline tariffs in data/rates."""
    folder = os.path.join(sync.data_dir(), "rates")
    out = {}
    for name in sorted(os.listdir(folder)) if os.path.isdir(folder) else []:
        if name.lower().endswith(sync.XLSX):
            try:
                carrier, eff, lines = tariff_changes.read_tariff(os.path.join(folder, name))
            except Exception:  # noqa: BLE001 - not an airline tariff, or unreadable
                continue
            out.setdefault(carrier, []).append((name, eff, lines))
    return out


def read_upload(up):
    """Parse an uploaded tariff (once per file) and compare it with the tariff in use."""
    if ss.get("_review", {}).get("id") != up.file_id:
        name = re.sub(r'[\\/:*?"<>|]+', "_", os.path.basename(up.name)).strip()
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, name)
            with open(path, "wb") as f:
                f.write(up.getvalue())
            try:
                carrier, eff, lines = tariff_changes.read_tariff(path)
            except Exception as e:  # noqa: BLE001 - shown to Ashita
                ss["_review"] = {"id": up.file_id, "name": name, "error": str(e)}
                return ss["_review"]
        current = tariffs_in_use(signature).get(carrier, [])
        old = tariff_changes.current_lines([(e, ls) for _, e, ls in current])
        ss["_review"] = {"id": up.file_id, "name": name, "data": up.getvalue(), "carrier": carrier, "eff": eff,
                         "current": [n for n, _, _ in current], "report": tariff_changes.compare(old, lines)}
    return ss["_review"]


def publish_tariff(review, replace):
    """Save the new tariff in Supabase and here, replacing the airline's old files or adding to them."""
    folder = os.path.join(sync.data_dir(), "rates")
    files = {f"rates/{n}": open(os.path.join(folder, n), "rb").read() for n in sorted(os.listdir(folder))
             if not (replace and n in review["current"])}
    files[f"rates/{review['name']}"] = review["data"]
    with st.spinner("Saving…"):
        cloud().upload(files)
    use_rates(files)
    ss["_published"] = f"{review['carrier']} tariff published: {review['name']}. Everyone sees it now."
    ss.pop("_review", None)
    ss["_upload_n"] = ss.get("_upload_n", 0) + 1  # clears the uploader
    st.rerun()


if ss.get("username") == ADMIN_USER:
    with st.sidebar:
        st.header("Update tariff")
        up = st.file_uploader("New airline tariff (.xlsx)", type=["xlsx"], key=f"tariff_{ss.get('_upload_n', 0)}",
                              help="IndiGo, Air India, Akasa, SpiceJet or Star Air. Keep the airline's file name; "
                                   "the effective date is read from it. You'll see what changes before publishing.")
        if ss.get("_published"):
            st.success(ss.pop("_published"))
        if cloud() is None:
            st.warning("Publishing is off: add the [supabase] section to Streamlit Secrets "
                       "(see secrets.toml.example).")
        elif restore_error:
            st.error(restore_error)
    if up:
        review = read_upload(up)
        with st.container(border=True):
            if review.get("error"):
                st.error(f"Couldn't read {review['name']} as an airline tariff: {review['error']}")
            else:
                rep, carrier = review["report"], review["carrier"]
                st.subheader(f"Review: {carrier} tariff" + (f" · eff. {dmy(review['eff'])}" if review["eff"] else ""))
                st.caption(f"{review['name']} compared with the {carrier} tariff in use: "
                           + (", ".join(review["current"]) or "none"))
                m = st.columns(4)
                m[0].metric("Lines changed", len(rep["changed"]))
                m[1].metric("New lines", len(rep["added"]))
                m[2].metric("Unchanged", rep["same"])
                m[3].metric("Not in the new file", len(rep["missing"]))
                if rep["changed"]:
                    st.dataframe([{"Line": tariff_changes.describe(k), "Change": " · ".join(
                        f"{label}: {'–' if a is None else a} → {'–' if b is None else b}" for label, a, b in diffs)}
                        for k, _, diffs in rep["changed"]], hide_index=True, width="stretch")
                for title, items in (("New lines", rep["added"]), ("Lines in use that aren't in the new file",
                                                                   rep["missing"])):
                    if items:
                        with st.expander(f"{title} ({len(items)})"):
                            st.dataframe([{"Line": tariff_changes.describe(k), "45+": r.get("RateQ45"),
                                           "100+": r.get("RateQ100")} for k, r in items], hide_index=True,
                                         width="stretch")
                partial = len(rep["missing"]) > (len(rep["changed"]) + rep["same"])
                mode = st.radio("How should it be used?", [
                    f"Update only the lines in this file — every other {carrier} line stays as it is",
                    f"Replace the whole {carrier} tariff — lines not in this file are removed"],
                    index=0 if partial else 1, key=f"mode_{review['id']}")
                problems = []
                if review["eff"] is None:
                    problems.append("No effective date found in the file name or the file. Rename it to include "
                                    "the date (e.g. 'Eff. 16th Oct 2026') and upload it again.")
                if cloud() is None:
                    problems.append("Publishing is off until Supabase is set up in Streamlit Secrets.")
                if review["eff"] and review["eff"] > today:
                    st.info(f"It takes effect on {dmy(review['eff'])}; until then the current rates stay in use.")
                for p in problems:
                    st.error(p)
                if st.button("Publish", type="primary", disabled=bool(problems)):
                    try:
                        publish_tariff(review, replace=mode.startswith("Replace"))
                    except Exception as e:  # noqa: BLE001 - shown to Ashita
                        st.error(f"Couldn't save, nothing was changed: {e}")


hubs, stations = queries.stations(conn)
if not stations:
    st.html(SHEET_CSS + TITLE)
    st.info("No data yet. Add the schedule and tariff files to the data folder (see README).")
    st.stop()

eng = TariffEngine(conn, today)
names = {s["Code"]: s["Name"] or "" for s in stations}
hub_codes = [s["Code"] for s in hubs] or list(names)
all_codes = queries.destinations(conn, hub_codes) or list(names)

# The route lives in ss["o"] / ss["d"]; the pickers are rebuilt (new key) when it's swapped.
if not ss.get("_from_link"):  # a shared link like ?from=DEL&to=BOM opens that route once per session
    ss["_from_link"] = True
    if st.query_params.get("from", "").upper() in hub_codes:
        ss["o"] = st.query_params["from"].upper()
    if st.query_params.get("to", "").upper() in all_codes:
        ss["d"] = st.query_params["to"].upper()
if ss.get("o") not in hub_codes:
    ss["o"] = "DEL" if "DEL" in hub_codes else hub_codes[0]
if ss.get("d") not in all_codes:
    ss["d"] = "CJB" if "CJB" in all_codes else all_codes[0]
ss.setdefault("pickers", 0)

st.html(SHEET_CSS + WIDGET_CSS + TITLE)


def swap():
    if ss["d"] in hub_codes and ss["o"] in all_codes:
        ss["o"], ss["d"] = ss["d"], ss["o"]
        ss["pickers"] += 1


def picked(which, key):
    ss[which] = ss[key]


n = ss["pickers"]
c = st.columns([5, 1, 5], vertical_alignment="bottom")
origin = c[0].selectbox("From", hub_codes, index=hub_codes.index(ss["o"]), key=f"origin_{n}", on_change=picked,
                        args=("o", f"origin_{n}"), format_func=lambda x: f"{x} · {names.get(x) or ''}")
c[1].button("⇄", key="swap", on_click=swap, width="stretch", help="Swap From and To", disabled=ss["d"] not in hub_codes)
dest = c[2].selectbox("To", all_codes, index=all_codes.index(ss["d"]), key=f"dest_{n}", on_change=picked,
                      args=("d", f"dest_{n}"), format_func=lambda x: f"{x} · {names.get(x) or ''}")
st.query_params.from_dict({"from": origin, "to": dest})

flights = queries.route_flights(conn, origin, dest, today) if origin != dest else []
results = eng.price_all(flights)
weekly = sum(queries.weekly_frequency(r.flight) for r in results)
status = "SAME CITY" if origin == dest else ("ACTIVE" if weekly else "NO SERVICE")

msg = {"SAME CITY": "⚠&nbsp; Please select two different cities as Origin and Destination.",
       "NO SERVICE": "✈&nbsp; No nonstop flights currently operate on this route. "
                     "Please try a different Origin / Destination combination."}.get(status, "")
if status == "ACTIVE" and not quote:
    msg = "Terminal & Surcharges aren't loaded yet, so CPKG is blank. Add the Allcargo sheet to data/allcargo."


def category(cat):
    """'PRIME' -> 'Prime', 'ALL' -> 'All', 'GCR' stays 'GCR'."""
    return {"PRIME": "Prime", "ALL": "All"}.get(cat, cat)


def price(r):
    """CPKG at 45+ and 100+. Akasa: its tariff rate (surcharges are already in it) + the OCDC add-on (origin
    outbound + destination inbound terminal charges); where the special-rates file covers the flight, its
    Direct AWB rate replaces the 100+ rate, + the same add-on. Other airlines: their tariff rate + the
    Allcargo Terminal & Surcharges for the flight. -> ({45: cell, 100: cell}, special)"""
    f = r.flight
    if f["Airline"] == "AKASA":
        addon, how = allcargo.ocdc_addon(ocdc, f)
        sp, sp_kind = allcargo.special_rate(special, f, r.product)
        cells = {}
        for brk in (45, 100):
            rate, used = r.rate_at(brk)
            what = f"Akasa tariff ({used}, surcharge included)"
            if brk == 100 and sp:
                rate, used, what = sp["ssp"], "100+", f"special Direct AWB rate (+100, {category(sp_kind)} flights)"
            if rate is None:
                cells[brk] = None
            elif addon is None:
                cells[brk] = {"v": None, "tip": f"{num(rate)} {what}; no OCDC terminal charges for these stations"}
            else:
                cells[brk] = {"v": round(rate + addon, 2), "used": used,
                              "tip": f"{num(rate)} {what} + {num(addon)} add-on (OCDC: {how})"}
        return cells, sp
    row, key = allcargo.quote_row(quote, f, r.product, with_number=True)
    ts = row["ts"] if row else None
    src = "its own flight row" if key == allcargo.flight_key(f["FlightNo"]) else f"the {key} row"
    cells = {}
    for brk in (45, 100):
        rate, used = r.rate_at(brk)
        if rate is None:
            cells[brk] = None
        elif ts is None:
            cells[brk] = {"v": None, "tip": f"{num(rate)} tariff ({used}); no Terminal & Surcharges for this "
                                            "flight in the Allcargo sheet"}
        else:
            cells[brk] = {"v": round(rate + ts, 2), "used": used,
                          "tip": f"{num(rate)} tariff ({used}) + {num(ts)} terminal & surcharges (Allcargo, {src})"}
    return cells, None


priced = [(r, *price(r)) for r in results]
lowest = {}  # weight break -> the lowest CPKG in that column
for brk in (45, 100):
    values = [cells[brk]["v"] for _, cells, _ in priced if cells[brk] and cells[brk]["v"] is not None]
    lowest[brk] = min(values) if values else None

rows_html = []
for r, cells, sp in priced:
    f = r.flight
    if r.status == STATUS_NO_TARIFF:
        product, pcls = "No tariff", "c muted"
    elif r.status == STATUS_NO_RATE:
        product, pcls = "No rate", "c muted"
    else:
        product, pcls = r.product or "", "c check" if r.status == STATUS_CHECK else "c"
    badge = ' <span class="sp" title="Akasa special rate">SPECIAL</span>' if sp else ""
    tds = []
    for brk in (45, 100):
        cell = cells[brk]
        if cell is None:
            tds.append('<td class="cpkg"></td>')
        elif cell["v"] is None:
            tds.append(f'<td class="cpkg muted" title="{esc(cell["tip"])}">–</td>')
        else:
            mark = f"<sup>{cell['used']}</sup>" if cell["used"] != f"{brk}+" else ""
            low = ' lowest' if cell["v"] == lowest[brk] else ""
            tip = cell["tip"] + (f"; lowest {brk}+ kg CPKG on this route" if low else "")
            tds.append(f'<td class="cpkg{low}" title="{esc(tip)}">{num(cell["v"])}{mark}</td>')
    rows_html.append(
        f"<tr><td class='air'><span class='dot' style='background:{AIRLINE_DOT.get(f['Airline'], MUTED)}'></span>"
        f"{esc(f['Airline'])}</td><td class='c fno'>{esc(f['FlightNo'])}</td>"
        f"<td class='{pcls}' title='{esc(r.match_note)}'>{esc(product)}{badge}</td>"
        f"<td class='c'>{esc(f['DepTime'])}</td>{''.join(tds)}</tr>")

route_specials = sorted((k[3], v) for k, v in (special or {"rows": {}})["rows"].items()
                        if (k[0], k[1]) == (allcargo.station(origin), allcargo.station(dest)))
special_msg = ""
if route_specials and status == "ACTIVE":
    special_msg = ("★&nbsp; Akasa special rate on this route: "
                   + " · ".join(f"{category(cat)} flights {num(v['ssp'])} (Direct AWB +100, plus add-on)"
                                for cat, v in route_specials))

route = (f"{esc(names.get(origin) or origin)} <span>{origin}</span>&nbsp; → &nbsp;"
         f"{esc(names.get(dest) or dest)} <span>{dest}</span>")
table = ""
if rows_html:
    table = f"""
<div class="xl-wrap"><table class="xl-table">
  <thead>
<tr><th rowspan="2" class="l">AIRLINE</th><th rowspan="2">FLT NO.</th><th rowspan="2">PRODUCT</th><th rowspan="2">ETD</th>
    <th class="cpkg" colspan="2">CPKG &nbsp;(₹/kg)</th></tr>
<tr><th class="cpkg">45+ kg</th><th class="cpkg">100+ kg</th></tr>
  </thead>
  <tbody>{''.join(rows_html)}</tbody>
</table></div>"""
fill, ink = STATUS_FILL[status]
st.html(SHEET_CSS + f"""
<div class="xl">
  <div class="xl-band">
<div><div class="k">Route</div><div class="v route">{route}</div></div>
<div><div class="k">Weekly flights</div><div class="v">{weekly}</div></div>
<div><div class="k">Flight count</div><div class="v">{len(results)}</div></div>
<div><div class="k">Status</div><div class="st" style="background:{fill};color:{ink}"><b>{status}</b></div></div>
  </div>
  {f'<div class="xl-msg">{msg}</div>' if msg else ''}
  {f'<div class="xl-special">{special_msg}</div>' if special_msg else ''}
  {table}
  <div class="xl-legend">
    <span><i style="background:{LOWEST_RED}"></i>Lowest CPKG in its column (45+ / 100+ kg)</span>
    <span><span class="sp" style="margin:0 6px 0 0">SPECIAL</span>Akasa special rate</span>
    <span><i style="background:{CHECK_AMBER}"></i>Check the rate condition (hover it)</span>
    <span>Hover a price for its breakdown</span>
  </div>
</div>""")
