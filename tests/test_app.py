import io

from conftest import make_indigo_file

from tariffapp.etl.loader import delete_batch, load_rate_file


def test_loader_skips_reload_and_delete_cascades(loaded, tmp_path):
    f = tmp_path / "8.1 6E Eff. 16th Aug 2026.xlsx"
    assert load_rate_file(loaded, str(f))["status"] == "skipped"
    bid = loaded.execute("SELECT BatchID FROM LoadBatches WHERE SourceFileName = ?", (f.name,)).fetchone()[0]
    assert loaded.execute("SELECT COUNT(*) FROM IndiGo_GCR WHERE BatchID = ?", (bid,)).fetchone()[0] == 3
    assert loaded.execute("SELECT COUNT(*) FROM IndiGo_Footnotes WHERE BatchID = ?", (bid,)).fetchone()[0] == 1
    delete_batch(loaded, bid)
    assert loaded.execute("SELECT COUNT(*) FROM IndiGo_GCR WHERE BatchID = ?", (bid,)).fetchone()[0] == 0
    assert load_rate_file(loaded, str(f))["status"] == "ok"


def test_schedule_import(loaded):
    assert loaded.execute("SELECT COUNT(*) FROM Flights").fetchone()[0] == 9  # NO_SERVICE row skipped
    hubs = {r[0] for r in loaded.execute("SELECT Code FROM Stations WHERE IsHub = 1")}
    assert hubs == {"AMD", "DEL", "BOM"}
    assert loaded.execute("SELECT TariffCode FROM Stations WHERE Code='TVM'").fetchone()[0] == "TRV"
    a = loaded.execute("SELECT AddOnPerKg, AddOnIncludes FROM Airlines WHERE Code='AKASA'").fetchone()
    assert a[0] is None and a[1] is None
    assert loaded.execute("SELECT COUNT(*) FROM NetworkSurcharges").fetchone()[0] == 5


def test_pages_render(app, loaded):
    c = app.test_client()
    for url in ["/", "/?origin=AMD&dest=DEL&weight=120&date=2026-09-01", "/?origin=AMD&dest=AMD",
                "/?origin=BOM&dest=COK", "/flights", "/flights?status=CHECK", "/exceptions", "/history",
                "/history?carrier=IndiGo&product=GCR&origin=AMD&dest=DEL", "/tariffs", "/tariffs?product=LEAN",
                "/notes", "/uploads", "/settings", "/api/lanes?origin=AMD"]:
        r = c.get(url)
        assert r.status_code == 200, url
    page = c.get("/?origin=AMD&dest=DEL&date=2026-09-01").get_data(as_text=True)
    assert "6E5245" in page and "LEAN" in page and "NO SERVICE" not in page
    assert "SAME CITY" in c.get("/?origin=AMD&dest=AMD").get_data(as_text=True)
    assert "NO SERVICE" in c.get("/?origin=BOM&dest=COK").get_data(as_text=True)
    hist = c.get("/history?carrier=IndiGo&product=GCR&origin=AMD&dest=DEL").get_data(as_text=True)
    assert "01-Jul-26" in hist and "16-Aug-26" in hist


def test_csv_export(app, loaded):
    r = app.test_client().get("/flights.csv?date=2026-09-01")
    lines = r.get_data(as_text=True).strip().splitlines()
    assert lines[0].startswith("Origin,Destination,Airline")
    assert len(lines) == 10


def test_empty_database_shows_get_started(app):
    assert "Get started" in app.test_client().get("/").get_data(as_text=True)


def test_upload_rate_file_via_web(app, conn, tmp_path):
    f = tmp_path / "x.xlsx"
    make_indigo_file(f, [("AMD", "DEL", "GCR", 1500, 70, 50, 40, None, None, None)])
    c = app.test_client()
    data = {"kind": "rates", "files": (io.BytesIO(f.read_bytes()), "9.0 6E Eff. 01st Sep 2026.xlsx")}
    r = c.post("/uploads", data=data, content_type="multipart/form-data", follow_redirects=True)
    assert "Loaded" in r.get_data(as_text=True)
    row = conn.execute("SELECT Carrier, EffectiveDate FROM LoadBatches").fetchone()
    assert tuple(row) == ("IndiGo", "2026-09-01 00:00:00")
    data = {"kind": "rates", "files": (io.BytesIO(f.read_bytes()), "9.0 6E Eff. 01st Sep 2026.xlsx")}
    r = c.post("/uploads", data=data, content_type="multipart/form-data", follow_redirects=True)
    assert "Already loaded" in r.get_data(as_text=True)


def test_settings_crud(app, loaded):
    c = app.test_client()
    c.post("/settings/surcharges", data={"Carrier": "IndiGo", "AppliesTo": "Origin", "Station": "amd",
                                          "RatePerKg": "2", "Active": "1", "Label": "Test", "action": "save"})
    row = loaded.execute("SELECT * FROM NetworkSurcharges WHERE Label='Test'").fetchone()
    assert row["Station"] == "AMD" and row["RatePerKg"] == 2
    page = c.get("/?origin=AMD&dest=DEL&date=2026-09-01").get_data(as_text=True)
    assert "Test ₹2" in page
    c.post("/settings/surcharges", data={"SurchargeID": row["SurchargeID"], "action": "delete"})
    assert loaded.execute("SELECT COUNT(*) FROM NetworkSurcharges WHERE Label='Test'").fetchone()[0] == 0

    c.post("/settings/airlines", data={"Code": "AKASA", "Name": "Akasa", "TariffCarrier": "", "AddOnPerKg": "15",
                                        "action": "save"})
    assert loaded.execute("SELECT AddOnPerKg FROM Airlines WHERE Code='AKASA'").fetchone()[0] == 15
    c.post("/settings/stations", data={"Code": "cok", "Name": "Kochi", "IsHub": "1", "action": "save"})
    assert loaded.execute("SELECT IsHub FROM Stations WHERE Code='COK'").fetchone()[0] == 1
