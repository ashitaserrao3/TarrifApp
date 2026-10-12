import csv
import datetime as dt

import pytest
from conftest import make_airindia_file

from tariffapp import recommend
from tariffapp.etl.loader import load_rate_file

AS_OF = dt.date(2026, 9, 1)


def by_lane(picks):
    return {(p.origin, p.dest): p for p in picks}


def add_air_india_competitor(conn, tmp_path):
    """An Air India flight on AMD-DEL, priced from a newer AI file."""
    conn.execute("INSERT INTO Flights (Origin, Destination, Airline, FlightNo, DepTime, Mon, Wed) "
                 "VALUES ('AMD', 'DEL', 'AIRINDIA', 'AI555', '11:00', 1, 1)")
    conn.commit()
    f = tmp_path / "8.4 AI effective 25-Aug-26.xlsx"
    make_airindia_file(f, [("AMD", "DEL", "GCR", 1500, 60, 50, 30, None, None, None)])
    assert load_rate_file(conn, str(f))["status"] == "ok"


def test_single_priced_airline_and_unpriced_listed(loaded):
    _, picks = recommend.recommend(loaded, AS_OF, 100)
    lane = by_lane(picks)[("AMD", "DEL")]
    # IndiGo's cheapest flight is 6E100 on LEAN: 26*100 freight + (5 sector + 4 fuel + 18 add-on)*100
    assert lane.winner.airline == "IndiGo" and lane.winner.best.flight["FlightNo"] == "6E100"
    assert lane.winner.total == pytest.approx(5300) and lane.winner.per_kg == pytest.approx(53)
    assert lane.winner.flights == 3  # 6E777 needs checking, so it doesn't compete
    assert lane.verdict == "Only priced airline" and lane.saving is None
    assert set(lane.unpriced) == {"Akasa Air"}
    assert ("AMD", "AMD") not in by_lane(picks)


def test_cheapest_airline_wins_with_saving(loaded, tmp_path):
    add_air_india_competitor(loaded, tmp_path)
    _, picks = recommend.recommend(loaded, AS_OF, 100)
    lane = by_lane(picks)[("AMD", "DEL")]
    # Air India: 30*100 + (5 SS + 12 add-on)*100 = 4700
    assert [o.airline for o in lane.options] == ["Air India", "IndiGo"]
    assert lane.verdict == "Cheapest" and lane.winner.total == pytest.approx(4700)
    assert lane.saving == pytest.approx(600) and lane.saving_per_kg == pytest.approx(6)
    assert lane.saving_pct == pytest.approx(600 / 5300 * 100)

    s = recommend.summary(picks)
    assert s["contested"] == 1 and s["total_saving"] == pytest.approx(600)
    assert dict(s["wins"])["Air India"] >= 1

    # minimum charges decide at small weights, and IndiGo's best flight changes: at 10 kg 6E396
    # (no sector surcharge) beats 6E100 -> AI 1500 + 17*10 vs IndiGo 1800 + (4 + 18)*10
    lane = by_lane(recommend.recommend(loaded, AS_OF, 10)[1])[("AMD", "DEL")]
    assert lane.winner.airline == "Air India" and lane.winner.total == pytest.approx(1670)
    assert lane.runner_up.best.flight["FlightNo"] == "6E396" and lane.runner_up.total == pytest.approx(2020)

    # before the new AI file was in force, Air India has no AMD-DEL rate
    lane = by_lane(recommend.recommend(loaded, dt.date(2026, 8, 21), 100)[1])[("AMD", "DEL")]
    assert lane.winner.airline == "IndiGo" and "Air India" in lane.unpriced


def test_origin_filter_and_bad_weight(loaded):
    _, picks = recommend.recommend(loaded, AS_OF, 100, origins={"DEL"})
    assert {p.origin for p in picks} == {"DEL"}
    with pytest.raises(ValueError):
        recommend.recommend(loaded, AS_OF, 0)


def test_picker_pages_and_csv(app, loaded, tmp_path):
    add_air_india_competitor(loaded, tmp_path)
    c = app.test_client()
    page = c.get("/picker?date=2026-09-01&weight=100").get_data(as_text=True)
    assert "Airline picker" in page and "Air India" in page and "₹600" in page
    assert c.get("/picker?contested=1&origin=AMD&weight=-5").status_code == 200
    body = c.get("/picker.csv?date=2026-09-01&weight=100").get_data(as_text=True).lstrip("﻿")
    rows = list(csv.DictReader(body.splitlines()))
    amd_del = next(r for r in rows if (r["Origin"], r["Destination"]) == ("AMD", "DEL"))
    assert (amd_del["Recommendation"], amd_del["Saving ₹"], amd_del["Runner-up"]) == ("Air India", "600.0", "IndiGo")


def test_recommend_cli(app, loaded, tmp_path):
    add_air_india_competitor(loaded, tmp_path)
    out = tmp_path / "picks.csv"
    res = app.test_cli_runner().invoke(args=["recommend", "--date", "2026-09-01", "--weight", "100",
                                             "--out", str(out)])
    assert res.exit_code == 0, res.output
    assert "Air India" in res.output and "saves Rs 600" in res.output
    assert out.read_text(encoding="utf-8-sig").startswith("Origin,Destination")
    assert app.test_cli_runner().invoke(args=["recommend", "--weight", "0"]).exit_code != 0
