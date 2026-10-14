import datetime as dt

import openpyxl

from tariffapp import quotes, sync


def make_quote_file(path):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["Issuer Index Pvt Ltd"])
    ws.append(["Some street, Mumbai"])
    ws.append(["ACME LOGISTICS"])
    ws.append(["Rate Quotation"])
    ws.append(["W.E.F. 01-10-2026"])
    ws.append(["Airline Charges Details"])
    ws.append(["Airline", "Minimum Freight", "Awb Chg", None, "DO Charge", None, "DC Charge", None,
               "Dry Ice Charge", None, "Val Charge", None, "Discount%"])
    ws.append(["Indigo", 1800, "Min. 500 / 2.00 Per kg", None, "Min. 150 / 1.00 Per kg", None, None, None,
               None, None, None, None, "7.00%"])
    ws.append(["AKASA", 1400, "Min. 300 / 1.00 Per kg", None, 300])
    ws.append([])
    ws.append(["Quotaion Details"])
    ws.append(["ORG", "DEST", "Airline", "Flight", "Commodity", "N Rate", "Q45", "Q100", "Q250", "Q300", "Q500",
               "Q1000", "Disc/Kg.", "Disc%", "Base+100 Rate", "Terminal&Surcharges", "Master+100", "Console Rate"])
    ws.append(["DEL", "BOM", "Indigo", "ALL", "GENERAL ", 90, 75, 40, None, None, None, None, None, "7%",
               37.2, 23.24, 60.44, 63])
    ws.append(["DEL", "BOM", "Indigo", "6E 0353", "GENERAL ", 90, 75, 60, None, None, None, None, None, "7%",
               55.8, 23.24, 79.04, 82])
    ws.append(["DEL", "BOM", "AKASA", "QP 1119", "GENERAL ", 75, 65, 53, None, None, None, None,
               "SSP & Console Rate as per attached special rates"])
    ws.append(["DEL", "BOM", "AKASA", "GCR", "GENERAL ", 58, 51, 41, None, None, None, None,
               "SSP & Console Rate as per attached special rates"])
    ws.append(["AMD", "CCU", "AKASA", "ALL", "GENERAL ", 58, 44, 36, None, None, None, None, None, None,
               34, 19.55, 53.55, 55])
    ws.append([])
    ws.append(["NOTE: 1. GST @ 18% will be applicable."])
    ws.append(["2. Payments by cheque favoring M/s.Index Logistics Pvt. Ltd. only."])
    ws.append(["3. Claims as per airline policy."])
    wb.save(path)


SPECIAL_TEXT = """AKASA - Special Rates w.e.f. 01-10-2026
Origin Destination Airline Flight Commodity +100 Console Rate
DEL BOM Akasa Prime GENERAL 17++++ 47
DEL BOM Akasa GCR GENERAL 17++++ 37
DEL AMD Akasa ALL GENERAL 19.5++++ 40
Notes:
1. All other charges shall be applicable as additional
(as per airlines tariff)
2. GST 18% will be applicable.
"""


def test_reads_quote_charges_rates_and_terms(tmp_path):
    make_quote_file(tmp_path / "q.xlsx")
    q = quotes.read_quote_workbook(tmp_path / "q.xlsx")
    assert q["customer"] == "ACME LOGISTICS" and q["effective"] == "2026-10-01"
    assert [c["Airline"] for c in q["charges"]] == ["INDIGO", "AKASA"]
    assert q["charges"][0]["DiscPct"] == "7.00%" and q["charges"][1]["DoCharge"] == "300"
    assert len(q["rates"]) == 5
    r = q["rates"][1]
    assert (r["Airline"], r["Flight"], r["DiscPct"], r["Console"]) == ("INDIGO", "6E353", 7, 82)
    assert q["rates"][2]["Note"].startswith("SSP & Console") and q["rates"][2]["Console"] is None
    # the issuer's name is never kept: not as customer, not in the terms
    assert q["notes"] == ["GST @ 18% will be applicable.", "Claims as per airline policy."]


def test_special_rates_text():
    s = quotes.parse_special_text(SPECIAL_TEXT)
    assert s["effective"] == "2026-10-01"
    assert [(r["Origin"], r["Destination"], r["Flight"], r["Base100"], r["Console"]) for r in s["rates"]] == [
        ("DEL", "BOM", "PRIME", 17, 47), ("DEL", "BOM", "GCR", 17, 37), ("DEL", "AMD", "ALL", 19.5, 40)]
    assert s["notes"][0] == "All other charges shall be applicable as additional (as per airlines tariff)"


def test_rate_for_each_flight(conn, tmp_path, monkeypatch):
    folder = tmp_path / "data"
    (folder / "quotes").mkdir(parents=True)
    make_quote_file(folder / "quotes" / "q.xlsx")
    monkeypatch.setattr(quotes, "read_special_pdf", lambda p: quotes.parse_special_text(SPECIAL_TEXT))
    (folder / "quotes" / "special.pdf").write_bytes(b"%PDF-1.4 stub")
    out = sync.sync_data_folder(conn, str(folder))
    assert out["quotes"] == ["q.xlsx (5 routes)", "special.pdf (3 special rates)"], out
    q = quotes.current_quote(conn, dt.date(2026, 10, 14))
    assert q["Customer"] == "ACME LOGISTICS"
    rows = quotes.route_quote(conn, q["QuoteID"], "DEL", "BOM")

    # a flight listed by number gets its own line, the others the ALL line
    assert quotes.rate_for_flight(rows, "INDIGO", "6E353")["Console"] == 82
    assert quotes.rate_for_flight(rows, "INDIGO", "6E999")["Console"] == 63
    # Akasa: special rates - the prime line for a listed (prime) flight, the GCR line for the rest
    prime = quotes.rate_for_flight(rows, "AKASA", "QP1119")
    assert (prime["Special"], prime["Base100"], prime["Console"]) == (True, 17, 47)
    assert quotes.rate_for_flight(rows, "AKASA", "QP1500")["Console"] == 37
    assert quotes.rate_for_flight(rows, "AKASA", "QP1500", product="PRIME")["Console"] == 47
    assert quotes.rate_for_flight(rows, "SPICEJET", "SG1") is None
    # airline charges only for the airlines asked for
    assert [c["Airline"] for c in quotes.charges(conn, q["QuoteID"], ["AKASA"])] == ["AKASA"]
    assert quotes.notes(conn, q["QuoteID"], "special")[1] == "GST 18% will be applicable."
    # unchanged files are not re-read
    assert sync.sync_data_folder(conn, str(folder))["quotes"] == []


def test_flight_key():
    assert quotes.flight_key("6E 0496") == "6E496"
    assert quotes.flight_key("QP-1925") == "QP1925"
    assert quotes.flight_key("S5 112") == "S5112"
    assert quotes.flight_key("ALL") == "ALL" and quotes.flight_key("Prime") == "PRIME"
