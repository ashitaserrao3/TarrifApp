"""The one-page dashboard renders, ranks flights cheapest-first and reacts to inputs."""

import os

import pytest

AppTest = pytest.importorskip("streamlit.testing.v1").AppTest

APP = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "streamlit_app.py")


def run(origin=None, dest=None):
    at = AppTest.from_file(APP, default_timeout=60)
    if origin:
        at.session_state["origin"] = origin
    if dest:
        at.session_state["dest"] = dest
    return at.run()


def table(at):
    df = at.dataframe[0].value
    return getattr(df, "data", df)  # Styler -> DataFrame


def test_empty_database(db_path):
    at = run()
    assert not at.exception
    assert any("Nothing loaded yet" in i.value for i in at.info)


def test_route_ranked_cheapest_first(loaded):
    # Today is after every loaded file, so the 16-Aug IndiGo cycle applies.
    at = run("AMD", "DEL")
    assert not at.exception, at.exception
    df = table(at)
    # 6E100 is LEAN (14:00): 100 kg uses the 100+ break, 26 + surcharges 9 + add-on 18 = 53/kg
    assert list(df["Flight"])[0] == "6E100"
    assert df.iloc[0]["₹/kg all-in"] == 53 and df.iloc[0]["Total ₹"] == 5300
    assert [m.value for m in at.metric][:3] == ["6E100 · 14:00", "₹53", "₹5,300"]
    assert at.metric[3].value == "25"
    # flights without a usable rate sink to the bottom with a note
    tail = df.set_index("Flight").loc[["6E777", "QP1000"], "Note"].tolist()
    assert tail[0].startswith("Check:") and tail[1] == "Tariff not loaded"
    assert set(df["Flight"].iloc[-2:]) == {"6E777", "QP1000"}


def test_weight_changes_the_answer(loaded):
    at = run("AMD", "DEL")
    at.number_input(key="weight").set_value(10.0).run()
    df = table(at)
    # 10 kg is under every minimum charge (1800) -> 180/kg freight + surcharges + add-on
    row = df.set_index("Flight").loc["6E100"]
    assert row["Total ₹"] == 1800 + (9 + 18) * 10


def test_swap_and_route_states(loaded):
    at = run("AMD", "DEL")
    at.button(key="swap").click().run()
    assert at.session_state["origin"] == "DEL" and at.session_state["dest"] == "AMD"
    at = run("AMD", "AMD")
    assert any("two different cities" in w.value for w in at.warning)
    at = run("BOM", "COK")
    assert any("No nonstop flights" in i.value for i in at.info)
