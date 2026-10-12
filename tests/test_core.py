from tariffapp.etl.loader import delete_batch, load_rate_file


def test_loader_skips_reload_and_delete_cascades(loaded, tmp_path):
    f = tmp_path / "8.1 6E Eff. 16th Aug 2026.xlsx"
    skipped = load_rate_file(loaded, str(f))
    assert skipped["status"] == "skipped" and str(skipped["effective_date"]) == "2026-08-16"
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
