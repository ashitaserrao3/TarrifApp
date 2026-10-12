"""The Excel-style app: the workbook's Dashboard sheet, live."""

import os
import re

import pytest

AppTest = pytest.importorskip("streamlit.testing.v1").AppTest

APP = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "streamlit_app.py")


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
    assert any("No data yet" in i.value for i in at.info)


def test_only_the_dashboard_sheet_is_shown(loaded):
    at = run()
    assert not at.exception, at.exception
    assert len(at.tabs) == 0 and len(at.dataframe) == 0
    assert "FLIGHT &nbsp;SCHEDULE" in sheet_html(at)


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


def test_visitors_cannot_upload(loaded):
    at = run()
    assert not at.exception
    assert len(at.get("file_uploader")) == 0
    assert any("Uploading is switched off" in c.value for c in at.caption)


def test_admin_signs_in_to_upload(loaded, monkeypatch):
    monkeypatch.setenv("TARIFFAPP_ADMIN_PASSWORD", "s3cret")
    at = run()
    assert len(at.get("file_uploader")) == 0  # signed out: no upload controls
    at.text_input(key="admin_pw").input("wrong")
    at.button(key="sign_in").click().run()
    assert any("Wrong password" in e.value for e in at.error)
    assert len(at.get("file_uploader")) == 0
    at.text_input(key="admin_pw").input("s3cret")
    at.button(key="sign_in").click().run()
    assert at.session_state["is_admin"] is True
    assert len(at.get("file_uploader")) == 2
    at.button(key="sign_out").click().run()
    assert len(at.get("file_uploader")) == 0


def test_app_builds_itself_from_the_data_folder(db_path, tmp_path):
    from conftest import make_indigo_file, make_schedule_file
    data = tmp_path / "data"
    (data / "schedule").mkdir(parents=True)
    (data / "rates").mkdir()
    make_schedule_file(data / "schedule" / "schedule.xlsx", [("AMD", "DEL", "INDIGO", "6E1", "10:00", "MTWTFSS")])
    make_indigo_file(data / "rates" / "8.1 6E Eff. 16th Aug 2026.xlsx",
                     [("AMD", "DEL", "GCR", 1800, 70, 50, 40, None, None, None)])
    at = run("AMD", "DEL")
    assert not at.exception, at.exception
    rows = dashboard_rows(at)
    assert rows == [("INDIGO", "6E1", "GCR", "10:00", "72", "62")]  # 50/40 + fuel 4 + add-on 18
