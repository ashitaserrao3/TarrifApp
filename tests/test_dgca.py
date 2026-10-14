import datetime as dt

from tariffapp import dgca, queries


def row(section, flight, freq, eff_from, eff_to, arr_from="", arr_time="", dep_to="", dep_time=""):
    return {"section": section, "flight": flight, "operator_code": "IGO", "aircraft": "A320N", "freq": freq,
            "arr_from": arr_from, "arr_time": arr_time, "dep_to": dep_to, "dep_time": dep_time,
            "eff_from": eff_from, "eff_to": eff_to}


SUMMER = ("2026-03-29", "2026-10-24")
META = {"file": "x.pdf", "season": "Summer", "operator": "INTERGLOBE AVIATION LTD."}

# Ahmedabad <-> Delhi and Ahmedabad -> Kochi, as the PDFs list them (sections by city name)
ROWS = [
    row("Ahmedabad", "6E100", "1234567", *SUMMER, dep_to="DEL", dep_time="14:00"),
    row("Delhi", "6E100", "1234567", *SUMMER, arr_from="AMD", arr_time="15:35"),
    row("Delhi", "6E101", "1234567", *SUMMER, dep_to="AMD", dep_time="17:00"),
    row("Ahmedabad", "6E101", "1234567", *SUMMER, arr_from="DEL", arr_time="18:35"),
    # 6E396 runs Mon-Fri until 30 Jun, then Sat-Sun from 1 Jul, at the same time: two rows, one flight
    row("Ahmedabad", "6E396", "12345", "2026-03-29", "2026-06-30", dep_to="DEL", dep_time="13:50"),
    row("Ahmedabad", "6E396", "67", "2026-07-01", "2026-10-24", dep_to="DEL", dep_time="13:50"),
    row("Ahmedabad", "6E396", "35", "2026-07-01", "2026-10-24", dep_to="DEL", dep_time="13:50"),
    row("Delhi", "6E396", "1234567", *SUMMER, arr_from="AMD", arr_time="15:25"),
    row("Ahmedabad", "6E700", "246", *SUMMER, dep_to="COK", dep_time="9:05"),
    row("Kochi", "6E700", "246", *SUMMER, arr_from="AMD", arr_time="11:30"),
]


def test_station_codes_are_worked_out_from_the_flights():
    assert dgca.infer_station_codes(ROWS) == {"Ahmedabad": "AMD", "Delhi": "DEL", "Kochi": "COK"}


def test_departures_become_flights_with_days_and_dates():
    flights = dgca.build_flights([(META, ROWS)])
    assert len(flights) == 6  # departure rows only
    f = next(x for x in flights if x["FlightNo"] == "6E700")
    assert (f["Origin"], f["Destination"], f["Airline"], f["DepTime"]) == ("AMD", "COK", "INDIGO", "09:05")
    assert [f[d] for d in dgca.DAYS] == [0, 1, 0, 1, 0, 1, 0]  # 2, 4, 6 = Tue, Thu, Sat
    assert (f["OriginName"], f["DestName"], f["Season"]) == ("Ahmedabad", "Kochi", "Summer")


def test_csv_round_trip(tmp_path):
    flights = dgca.build_flights([(META, ROWS)])
    dgca.write_csv(tmp_path / "schedule.csv", "abc", flights)
    digest, back = dgca.read_csv(tmp_path / "schedule.csv")
    assert digest == "abc" and len(back) == len(flights) and back[0]["FlightNo"] == flights[0]["FlightNo"]


def _load(conn):
    conn.execute("INSERT INTO Stations (Code, Name, IsHub, SortOrder) VALUES ('AMD', 'Ahmedabad', 1, 1)")
    n = dgca.import_flights(conn, dgca.build_flights([(META, ROWS)]), "digest-1")
    assert n == 6
    return conn


def test_route_flights_use_the_rows_valid_on_the_day(conn):
    _load(conn)
    june = queries.route_flights(conn, "AMD", "DEL", dt.date(2026, 6, 15))
    assert [(f["FlightNo"], f["DepTime"]) for f in june] == [("6E396", "13:50"), ("6E100", "14:00")]
    f396 = june[0]
    assert [f396[d] for d in dgca.DAYS] == [1, 1, 1, 1, 1, 0, 0]
    august = queries.route_flights(conn, "AMD", "DEL", dt.date(2026, 8, 3))
    f396 = next(f for f in august if f["FlightNo"] == "6E396")
    assert [f396[d] for d in dgca.DAYS] == [0, 0, 1, 0, 1, 1, 1]  # the two July rows merged: Wed, Fri, Sat, Sun


def test_schedule_info_and_end_of_season(conn):
    _load(conn)
    info = queries.schedule_info(conn, dt.date(2026, 8, 3))
    assert info["source"] == "dgca" and info["label"] == "DGCA Summer 2026" and not info["expired"]
    assert info["date"] == dt.date(2026, 8, 3)
    late = queries.schedule_info(conn, dt.date(2026, 11, 2))
    assert late["expired"] and late["date"] == dt.date(2026, 10, 24)
    # after the season the app still shows the schedule's last day instead of an empty page
    assert queries.route_flights(conn, "AMD", "DEL", dt.date(2026, 11, 2))
    # new airports are added with their city names, without becoming origins
    assert conn.execute("SELECT Name, IsHub FROM Stations WHERE Code='COK'").fetchone()[:] == ("Kochi", 0)


def test_workbook_import_keeps_dgca_flights(conn, tmp_path):
    from conftest import make_schedule_file

    from tariffapp.importers import import_schedule_workbook
    _load(conn)
    make_schedule_file(tmp_path / "s.xlsx", [("AMD", "DEL", "INDIGO", "6E1", "10:00", "MTWTFSS")])
    import_schedule_workbook(conn, str(tmp_path / "s.xlsx"))
    assert conn.execute("SELECT COUNT(*) FROM Flights WHERE Source='dgca'").fetchone()[0] == 6
    assert conn.execute("SELECT COUNT(*) FROM Flights WHERE Source='workbook'").fetchone()[0] == 1
    # with DGCA loaded, the route shows DGCA flights, not the workbook's
    flights = queries.route_flights(conn, "AMD", "DEL", dt.date(2026, 8, 3))
    assert "6E1" not in {f["FlightNo"] for f in flights}


def test_multi_stop_flights_do_not_confuse_airport_codes():
    # IX1521: Kolkata -> Agartala -> Bagdogra; small airports only linked by such flights
    rows = ROWS + [
        row("Kolkata", "IX1521", "1234567", *SUMMER, dep_to="IXA", dep_time="11:20"),
        row("Agartala", "IX1521", "1234567", *SUMMER, arr_from="CCU", arr_time="12:15"),
        row("Agartala", "IX1521", "1234567", *SUMMER, dep_to="IXB", dep_time="12:55"),
        row("Bagdogra", "IX1521", "1234567", *SUMMER, arr_from="IXA", arr_time="14:05"),
        row("Bagdogra", "IX1522", "1234567", *SUMMER, dep_to="IXA", dep_time="14:45"),
        row("Agartala", "IX1522", "1234567", *SUMMER, arr_from="IXB", arr_time="15:50"),
        row("Kolkata", "6E900", "1234567", *SUMMER, dep_to="DEL", dep_time="07:00"),
        row("Delhi", "6E900", "1234567", *SUMMER, arr_from="CCU", arr_time="09:10"),
    ]
    codes = dgca.infer_station_codes(rows)
    assert (codes["Agartala"], codes["Bagdogra"], codes["Kolkata"]) == ("IXA", "IXB", "CCU")
