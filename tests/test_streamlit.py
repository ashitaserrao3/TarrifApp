"""The Excel-style app: the Dashboard sheet and the workbook's other sheets as tabs."""

import os
import re

import pytest

AppTest = pytest.importorskip("streamlit.testing.v1").AppTest

APP = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "streamlit_app.py")
SHEETS = ["Dashboard", "Add-on Rates", "Raw Data", "OD Pair Summary", "Network Surcharges", "Flight Rates",
          "Rate Exceptions"]


def run(origin=None, dest=None, **query):
    at = AppTest.from_file(APP, default_timeout=60)
    if origin:
        at.session_state["origin"] = origin
    if dest:
        at.session_state["dest"] = dest
    for k, v in query.items():
        at.query_params[k] = v
    return at.run()


def sheet_html(at):
    return " ".join(getattr(h.proto, "body", "") for h in at.get("html"))


def dashboard_rows(at):
    """[(airline, flight, product, etd, cpkg45, cpkg100)] from the Dashboard table."""
    body = sheet_html(at)
    tbody = body.split("<tbody>", 1)[1].split("</tbody>", 1)[0] if "<tbody>" in body else ""
    rows = []
    for tr in re.findall(r"<tr>(.*?)</tr>", tbody, re.S):
        cells = [re.sub(r"<sup>.*?</sup>|<[^>]+>", "", td).strip() for td in re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)]
        rows.append(tuple(cells))
    return rows


def test_empty_database(db_path):
    at = run()
    assert not at.exception
    assert any("Nothing loaded yet" in i.value for i in at.info)


def test_tabs_match_the_workbook_sheets(loaded):
    at = run()
    assert not at.exception, at.exception
    assert [t.label for t in at.tabs] == SHEETS
    assert len(at.dataframe) >= 5  # every sheet tab renders its table


def test_dashboard_matches_excel_layout_and_values(loaded):
    at = run("AMD", "DEL")
    body = sheet_html(at)
    for text in ("FLIGHT &nbsp;SCHEDULE", "ROUTE", "WEEKLY FLIGHTS", "FLIGHT COUNT", "ACTIVE", "CPKG &nbsp;(₹/kg)"):
        assert text in body
    assert ">25<" in body and ">5<" in body  # 7+3+1+7+7 weekly flights, 5 flight numbers
    rows = {r[1]: r for r in dashboard_rows(at)}
    # 6E100 is LEAN at 14:00: 45+ = 55 + surcharges 9 + add-on 18; 100+ = 26 + 9 + 18
    assert rows["6E100"] == ("INDIGO", "6E100", "LEAN", "14:00", "82", "53")
    assert rows["6E396"][2] == "GCR"
    assert rows["QP1000"][2:] == ("No tariff", "09:00", "", "")
    assert "class='c check'" in body  # 6E396 (departure time '0') is flagged amber, as in Excel


def test_same_city_and_no_service_messages(loaded):
    body = sheet_html(run("AMD", "AMD"))
    assert "SAME CITY" in body and "select two different cities" in body
    body = sheet_html(run("BOM", "COK"))
    assert "NO SERVICE" in body and "No nonstop flights" in body


def test_shared_link_opens_route(loaded):
    at = run(**{"from": "del", "to": "COK"})
    assert (at.session_state["origin"], at.session_state["dest"]) == ("DEL", "COK")
    assert {r[1] for r in dashboard_rows(at)} == {"AI1715", "AI2458"}
