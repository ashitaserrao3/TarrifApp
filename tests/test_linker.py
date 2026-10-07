import datetime as dt

import pytest

from tariffapp.linker import (STATUS_CHECK, STATUS_NO_RATE, STATUS_NO_TARIFF, STATUS_OK, TariffEngine,
                              norm_flight, parse_condition, remark_end_date, surcharge_applies)


@pytest.mark.parametrize("text,kind", [
    (None, "all"),
    ("", "all"),
    ("For All Flights departed between 00:01 TO 09:00 HRS", "window"),
    ("For all flights departed between 11:01 hrs to 24:00 hrs.", "window"),
    ("For All Flights departed between 09:01-00:00 HRS", "window"),
    ("For All Flights departed between 10:01HRS TO 24:00 HRS", "window"),
    ("Applicable for 6E 5245", "flights"),
    ("Applicable for 6E632 & 6E446", "flights"),
    ("All Flights Except 6E135", "except"),
    ("For All Flights (Except 6E413 & 6E887)", "except"),
    ("Except for 6E 6422", "except"),
    ("Applicable Flights 01:00 to 08:00 Hours\n(AI-2873, AI-2625)", "flights"),
    ("All Flights Excluding Prime flights \n(AI-2653, IX-1170)", "except"),
    ("Flight Number AI-2449 AI-1706", "flights"),
    ("IX-1463", "flights"),
    ("Something odd", "unknown"),
])
def test_condition_kinds(text, kind):
    assert parse_condition(text).kind == kind


def test_condition_flight_lists_and_exclusions():
    c = parse_condition("Applicable Flights 00:00 to 09:00 Hours\n(AI-0817, AI-2959)\nExcluding AI-0423")
    assert c.flights == {"AI817", "AI2959"}
    assert c.excluded == {"AI423"}
    assert c.matches("AI817", 300)
    assert not c.matches("AI423", 300)
    c = parse_condition("All Flights Except 6E 6877, 6E 6564")
    assert c.excluded == {"6E6877", "6E6564"}
    assert c.matches("6E1", None) and not c.matches("6E6564", 0)


def test_time_windows_including_midnight_wrap():
    early = parse_condition("For All Flights departed between 01:01 TO 06:30 HRS")
    late = parse_condition("For All Flights departed between 06:31 TO 01:00 HRS")
    for hhmm, e, l in [(5 * 60, True, False), (6 * 60 + 30, True, False), (6 * 60 + 31, False, True),
                       (23 * 60, False, True), (30, False, True), (61, True, False)]:
        assert early.matches("6E1", hhmm) is e, hhmm
        assert late.matches("6E1", hhmm) is l, hhmm
    day = parse_condition("For All Flights departed between 00:01 TO 09:00 HRS")
    assert day.matches("6E1", 0) and day.matches("6E1", 540) and not day.matches("6E1", 541)
    assert day.matches("6E1", None) is None  # can't decide without a valid time


def test_norm_flight():
    assert norm_flight("AI-0441") == "AI441"
    assert norm_flight("6E 5245") == "6E5245"
    assert norm_flight("ix1054") == "IX1054"
    assert norm_flight("6e+105") is None  # odd schedule entry - deliberately not a 6E105 match


def test_remark_end_dates():
    assert remark_end_date("Increase commercial surcharge from INR 5 to INR 10 per kg Effective 16-Jul to 31-Aug 2026",
                           2026) == dt.date(2026, 8, 31)
    assert remark_end_date("Introduction of SS Surcharge of INR 6/kg Eff. 01-Jul to 17-Aug-26", 2026) == \
        dt.date(2026, 8, 17)
    assert remark_end_date("Effective 21-Jul-26 till 31-Aug-26 (Flight Number AI-2906)", 2026) == dt.date(2026, 8, 31)
    assert remark_end_date("Introduction of new sector tariff", 2026) is None


def test_surcharge_applies():
    assert surcharge_applies("INR 3 / KG", "AI1")
    assert not surcharge_applies("INR 5/- per kg (PEF) on 6E975, 6E952", "6E975")
    assert surcharge_applies("INR 5 per kg on 6E975", "6E975")
    assert not surcharge_applies("INR 5 per kg on 6E975", "6E1")


def _by_flight(conn, as_of):
    eng = TariffEngine(conn, as_of)
    flights = [dict(r) for r in conn.execute("SELECT * FROM Flights")]
    return {r.flight["FlightNo"]: r for r in eng.price_all(flights)}


def test_engine_indigo_windows_flights_and_surcharges(loaded):
    res = _by_flight(loaded, dt.date(2026, 9, 29))
    early = res["6E396"]  # 05:00 -> GCR window
    assert (early.status, early.product, early.rates["N"]) == (STATUS_OK, "GCR", 71)
    assert early.match_note == "Departure time within window"
    assert [s.label for s in early.surcharges] == ["Fuel"]  # inactive AMD origin surcharge is ignored

    lean = res["6E100"]  # 14:00 -> LEAN window; PEF flight surcharge must NOT apply
    assert (lean.product, lean.rates["45"]) == ("LEAN", 55)
    assert lean.surcharge_summary == "Sector ₹5 + Fuel ₹4"
    assert lean.surcharge_total == 9
    assert lean.addon == 18
    assert lean.cpkg(45) == (55 + 9 + 18, "45+")

    special = res["6E5245"]  # flight-specific beats the time window
    assert (special.product, special.rates["N"], special.match_note) == ("GCR", 90, "Flight-specific rate")

    bad_time = res["6E777"]
    assert bad_time.status == STATUS_CHECK and "not a valid time" in bad_time.match_note

    assert res["QP1000"].status == STATUS_NO_TARIFF

    tvm = res["6E50"]  # schedule TVM -> tariff TRV
    assert tvm.status == STATUS_OK and tvm.rates["N"] == 68
    assert tvm.rate_at(45) == (68, "N")  # no 45+ break -> falls back to N


def test_engine_air_india_prime_and_surcharges(loaded):
    res = _by_flight(loaded, dt.date(2026, 8, 25))
    prime = res["AI1715"]
    assert (prime.product, prime.match_note) == ("PRIME", "Flight-specific rate")
    # Flight surcharge from the rate row + SS + Onam (route surcharge valid 07-31 Aug)
    assert prime.surcharge_summary == "Flight ₹10 + SS ₹5 + Onam ₹5"
    gcr = res["AI2458"]
    assert gcr.product == "GCR" and gcr.surcharges[0].label == "Sector"
    assert gcr.surcharges[0].flag == ""  # 31-Aug end date not yet passed on 25-Aug

    later = _by_flight(loaded, dt.date(2026, 9, 29))
    assert later["AI2458"].surcharges[0].flag.startswith("ended 31-Aug-26")
    assert "Onam" not in later["AI2458"].surcharge_summary

    bom = later["AI2421"]
    assert bom.product == "PRIME"
    # commodity-only commercial surcharge on the GCR row isn't relevant; PRIME row has none; To-BOM applies
    assert bom.surcharge_summary == "SS ₹5 + To-BOM ₹6"


def test_engine_uses_rate_file_in_force_on_date(loaded):
    july = _by_flight(loaded, dt.date(2026, 7, 15))
    assert july["6E396"].rates["N"] == 70 and july["6E396"].effective_date == dt.date(2026, 7, 1)
    assert july["AI2458"].status == STATUS_NO_TARIFF  # Air India file only effective from 20-Aug
    before_all = _by_flight(loaded, dt.date(2026, 1, 1))
    assert before_all["6E396"].status == STATUS_NO_TARIFF


def test_no_rate_for_route(loaded):
    loaded.execute("INSERT INTO Flights (Origin, Destination, Airline, FlightNo, DepTime) "
                   "VALUES ('AMD','COK','INDIGO','6E9','10:00')")
    res = _by_flight(loaded, dt.date(2026, 9, 1))
    assert res["6E9"].status == STATUS_NO_RATE


def test_freight_charge_minimum_and_higher_break(loaded):
    r = _by_flight(loaded, dt.date(2026, 9, 1))["6E396"]  # Min 1800, N 71, Q45 57, Q100 43
    assert r.freight_charge(10) == 1800             # minimum charge
    assert r.freight_charge(50) == 57 * 50
    assert r.freight_charge(95) == 43 * 100         # cheaper to charge at the 100 kg break
    assert r.total_cost(200) == 43 * 200 + (4 + 18) * 200
