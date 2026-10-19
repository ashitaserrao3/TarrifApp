import os
import sys

import openpyxl

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import allcargo  # noqa: E402

HEADER = ["ORG", "DEST", "Airline", "Flight", "Commodity", "N Rate", "Q45", "Q100", "Q250", "Q300", "Q500", "Q1000",
          "Disc/Kg.", "Disc%", "Base+100 Rate", "Terminal&Surcharges", "Master+100", "Console Rate"]


def make_quote(path, rows):
    wb = openpyxl.Workbook()
    ws = wb.active
    for line in ("ALLCARGO LOGISTICS LIMITED", "Rate Quotation", "W.E.F. 01-10-2026", "Quotaion Details"):
        ws.append([line])
    ws.append(HEADER)
    for o, d, a, flight, ts, note in rows:
        ws.append([o, d, a, flight, "GENERAL ", 98, 75, 65, None, None, None, None, note, "14%", 55.9, ts, None, None])
    wb.save(path)


def flight(o, d, airline, no):
    return {"Origin": o, "Destination": d, "Airline": airline, "FlightNo": no}


def test_rows_are_matched_by_flight_number_first(tmp_path):
    path = tmp_path / "ALLCARGO.xlsx"
    make_quote(path, [
        ("DEL", "CJB", "Air India", "AI 1721", 31.65, None),
        ("DEL", "CJB", "Air India", "ALL", 30.00, None),
        ("DEL", "CJB", "Indigo", "6E 0496", 25.00, None),
        ("DEL", "CJB", "Indigo", "ALL", 23.50, None),
        ("DEL", "TRV", "STAR AIR", "ALL", 12.00, None),
    ])
    assert allcargo.is_quote(path)
    q = allcargo.read_quote(path)
    assert q["wef"] == "01-10-2026"
    row = allcargo.quote_row
    assert row(q, flight("DEL", "CJB", "AIRINDIA", "AI1721"), "PRIME")[1] == "AI1721"
    assert row(q, flight("DEL", "CJB", "AIRINDIA", "AI1753"), "GCR")[0]["ts"] == 30.00
    assert row(q, flight("DEL", "CJB", "INDIGO", "6E496"), "GCR")[0]["ts"] == 25.00  # '6E 0496' in the sheet
    assert row(q, flight("DEL", "TVM", "STARAIR", "S5101"), "GCR")[0]["ts"] == 12.00  # TRV in the sheet
    assert row(q, flight("DEL", "BOM", "INDIGO", "6E1"), "GCR") == (None, None)
    assert row(None, flight("DEL", "CJB", "INDIGO", "6E1"), "GCR") == (None, None)


def test_special_rate_by_route_and_the_tariffs_product():
    special = {"rows": {("BOM", "DEL", "AKASA", "PRIME"): {"ssp": 26.0, "console": 46.0},
                        ("DEL", "BOM", "AKASA", "PRIME"): {"ssp": 17.0, "console": 47.0},
                        ("DEL", "BOM", "AKASA", "GCR"): {"ssp": 16.0, "console": 37.0},
                        ("AMD", "PNQ", "AKASA", "ALL"): {"ssp": 29.0, "console": 49.0}}}
    sr = allcargo.special_rate
    assert sr(special, flight("BOM", "DEL", "AKASA", "QP1109"), "PRIME")[1] == "PRIME"
    assert sr(special, flight("BOM", "DEL", "AKASA", "QP1127"), "GCR") == (None, None)  # Prime-only route
    assert sr(special, flight("DEL", "BOM", "AKASA", "QP1119"), "GCR")[0]["ssp"] == 16.0
    assert sr(special, flight("DEL", "BOM", "AKASA", "QP1119"), "PRIME")[0]["ssp"] == 17.0
    assert sr(special, flight("AMD", "PNQ", "AKASA", "QP1911"), "GCR")[1] == "ALL"
    assert sr(special, flight("AMD", "CCU", "AKASA", "QP1925"), "PRIME") == (None, None)  # route not in the file
    assert sr(None, flight("DEL", "BOM", "AKASA", "QP1119"), "GCR") == (None, None)


def test_ocdc_addon_is_origin_outbound_plus_destination_inbound(tmp_path):
    wb = openpyxl.Workbook()
    wb.active.title = "Tariff Rate"
    ws = wb.create_sheet("OCDC")
    for row in (
        [None, None, "Outbound", None, "Inbound", None],
        ["Station", "Category", "Rate Kgs", "Minimum Charges", "Rate Kgs", "Minimum Chagres"],
        ["DEL", "X-Ray Charges ( Screening )", "Rs.1.60/-kg", "Rs.155/-", None, None],
        [None, "X-Ray Charges ( Certification )", "Rs.1.60/-kg", "Rs.155/-", None, None],
        [None, "Unitization   De-Unitization Charges", "Rs.1.75/-kg", "Rs.50/-", "Rs.1.45/-kg", "Rs.50/-"],
        [None, "Unitization  De-Unitization (Special Cargo i.e AVI,PEF)", "Rs.2.00/-kg", "Rs.50/-", "Rs.1.65/-kg", None],
        [None, "TSP CHARGES (General Cargo)", "Rs.1.65/-kg", "Rs.50/-", "Rs.1.65/-kg", "Rs.50/-"],
        [None, "DGR Shipments", None, "Rs.3000/-", None, None],
        [None, "Demurrage Charges  Storage(Per Day)", None, None, None, None],
        [None, "General Cargo & Courier", "Rs.1.45/-kg", "Rs.50/-", "Rs.1.45/-kg", "Rs.50/-"],
        ["BOM", "X-Ray Charges", "Rs.2.25/-kg", "Rs.195/-", None, None],
        [None, "Unitization/ De-Unitization (General Cargo)", "Rs.2.25/-kg", "Rs.315/-", "Rs.2.25/-kg", "Rs.315/-"],
        ["IXB", "TSP Charges (General Cargo) ( Perishable Cargo )", "Rs.5.50/-kg", "Rs.100/-", "Rs.4.50/-kg", None],
        [],
        ["CHARGES  FOR NON- CARGO TERMINAL STATIONS ", None, None, None, None, None],
        ["AYJ", "Handling charges", "Rs.2.00/-", "Rs.100/- ", "Rs.1.00/-", "Rs.100/- "],
    ):
        ws.append(row)
    path = tmp_path / "Akasa Tariff.xlsx"
    wb.save(path)
    ocdc = allcargo.read_ocdc(path)
    assert round(sum(r for _, r in ocdc["DEL"]["out"]), 2) == 6.6
    assert round(sum(r for _, r in ocdc["DEL"]["in"]), 2) == 3.1
    assert allcargo.ocdc_addon(ocdc, flight("DEL", "BOM", "AKASA", "QP1119"))[0] == 8.85  # 6.60 + 2.25
    assert allcargo.ocdc_addon(ocdc, flight("BOM", "IXB", "AKASA", "QP1"))[0] == 9.0      # 4.50 + 4.50
    assert allcargo.ocdc_addon(ocdc, flight("AYJ", "DEL", "AKASA", "QP2"))[0] == 5.1      # 2.00 + 3.10
    assert allcargo.ocdc_addon(ocdc, flight("GOI", "PNY", "AKASA", "QP3")) == (None, None)


def test_airline_tariffs_are_not_taken_for_the_quote(tmp_path):
    wb = openpyxl.Workbook()
    wb.active.append([" Common User Terminal Charges (16-Apr-2026)"])  # as in SpiceJet's CUT Charges sheet
    wb.save(tmp_path / "sg.xlsx")
    assert not allcargo.is_quote(tmp_path / "sg.xlsx")

