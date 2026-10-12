"""Streamlit front end: every page renders against a loaded database, and the
interactive bits (weight, reverse, settings validation) behave."""

import datetime as dt
import os

import pytest

st_testing = pytest.importorskip("streamlit.testing.v1")
AppTest = st_testing.AppTest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def page_app(page):
    script = f"""
import sys
sys.path.insert(0, {ROOT!r})
import streamlit_app
streamlit_app.{page}()
"""
    return AppTest.from_string(script, default_timeout=60)


@pytest.fixture
def st_db(app, loaded, monkeypatch):
    monkeypatch.setenv("TARIFFAPP_DB", app.config["DATABASE"])
    return loaded


@pytest.mark.parametrize("page", ["page_route", "page_flights", "page_history", "page_tariffs", "page_notes",
                                  "page_exceptions", "page_upload", "page_settings",
                                  "page_picker"])
def test_every_page_renders(st_db, page):
    at = page_app(page).run()
    assert not at.exception, at.exception


def test_route_lookup_prices_and_reverses(st_db):
    at = page_app("page_route")
    at.session_state["origin"] = "AMD"
    at.session_state["dest"] = "DEL"
    at.run()
    at.date_input(key="route_date").set_value(dt.date(2026, 9, 1)).run()
    assert [m.value for m in at.metric][1:] == ["25", "5", "ACTIVE"]  # 7+3+1+7+7 flights a week
    df = at.dataframe[0].value
    df = getattr(df, "data", df)  # Styler -> DataFrame
    assert set(df["Flight"]) == {"6E396", "6E100", "6E5245", "6E777", "QP1000"}
    row = df[df["Flight"] == "6E100"].iloc[0]
    assert (row["Product"], row["CPKG 45+"]) == ("LEAN", 55 + 9 + 18)

    at.number_input[0].set_value(200.0).run()
    df = at.dataframe[0].value
    df = getattr(df, "data", df)
    assert "Total 200 kg ₹" in df.columns

    at.button[0].click().run()  # reverse: DEL is a hub, so DEL -> AMD
    assert at.session_state["origin"] == "DEL" and at.session_state["dest"] == "AMD"
    assert not at.exception


def test_same_city_and_no_service(st_db):
    at = page_app("page_route")
    at.session_state["origin"] = "AMD"
    at.session_state["dest"] = "AMD"
    at.run()
    assert any("two different cities" in w.value for w in at.warning)
    at.session_state["dest"] = "COK"
    at.run()
    assert any("No nonstop flights" in i.value for i in at.info)


def test_empty_database_route_page(app, monkeypatch):
    monkeypatch.setenv("TARIFFAPP_DB", app.config["DATABASE"])
    at = page_app("page_route").run()
    assert any("No flight schedule loaded" in i.value for i in at.info)


def test_settings_store_validation(st_db):
    from tariffapp.settings_store import save_airlines, save_network_surcharges, save_stations

    with pytest.raises(ValueError, match="need a station"):
        save_network_surcharges(st_db, [{"Carrier": "IndiGo", "AppliesTo": "Origin", "RatePerKg": 3}])
    with pytest.raises(ValueError, match="DEL-COK"):
        save_network_surcharges(st_db, [{"Carrier": "IndiGo", "AppliesTo": "Route", "Station": "DEL", "RatePerKg": 3}])
    with pytest.raises(ValueError, match="before Valid from"):
        save_network_surcharges(st_db, [{"Carrier": "IndiGo", "AppliesTo": "All", "RatePerKg": 3,
                                         "ValidFrom": dt.date(2026, 9, 1), "ValidTo": dt.date(2026, 8, 1)}])
    # a failed save leaves the table untouched
    assert st_db.execute("SELECT COUNT(*) FROM NetworkSurcharges").fetchone()[0] == 5

    n = save_network_surcharges(st_db, [
        {"Carrier": "IndiGo", "AppliesTo": "All", "Station": "ignored", "RatePerKg": "4", "Active": True,
         "ValidFrom": dt.date(2026, 4, 2), "ValidTo": float("nan"), "Label": "Fuel"},
        {"Carrier": None, "AppliesTo": "All", "Station": "", "RatePerKg": float("nan"), "Active": True},  # blank row
    ])
    assert n == 1
    row = st_db.execute("SELECT * FROM NetworkSurcharges").fetchone()
    assert (row["Station"], row["RatePerKg"], row["ValidFrom"], row["ValidTo"]) == (None, 4.0, "2026-04-02", None)

    with pytest.raises(ValueError, match="listed twice"):
        save_airlines(st_db, [{"Code": "INDIGO"}, {"Code": "indigo"}])
    assert save_airlines(st_db, [{"Code": "indigo", "TariffCarrier": "IndiGo", "AddOnPerKg": 20}]) == 1
    assert st_db.execute("SELECT Code, AddOnPerKg FROM Airlines").fetchall()[0][:] == ("INDIGO", 20.0)

    assert save_stations(st_db, [{"Code": "tvm", "IsHub": False, "TariffCode": "trv"},
                                 {"Code": "AMD", "IsHub": True}]) == 2
    assert st_db.execute("SELECT TariffCode FROM Stations WHERE Code='TVM'").fetchone()[0] == "TRV"
