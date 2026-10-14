import os
import shutil

from conftest import make_indigo_file, make_schedule_file

from tariffapp import sync


def test_sync_loads_new_files_once_and_reimports_changed_schedule(conn, tmp_path):
    data = tmp_path / "data"
    (data / "schedule").mkdir(parents=True)
    (data / "rates").mkdir()
    make_schedule_file(data / "schedule" / "ATC_AOC_Flight_Schedule.xlsx",
                       [("AMD", "DEL", "INDIGO", "6E1", "10:00", "MTWTFSS")])
    make_indigo_file(data / "rates" / "8.1 6E Eff. 16th Aug 2026.xlsx",
                     [("AMD", "DEL", "GCR", 1800, 70, 50, 40, None, None, None)])

    first = sync.sync_data_folder(conn, str(data))
    assert first["schedule"].startswith("ATC_AOC_Flight_Schedule.xlsx (1 flights")
    assert first["rates_loaded"] == ["8.1 6E Eff. 16th Aug 2026.xlsx"] and not first["errors"]

    again = sync.sync_data_folder(conn, str(data))
    assert again["schedule"] is None and again["rates_loaded"] == [] and again["rates_skipped"] == 1

    # a changed workbook is re-imported; a new rate file is added to the history
    sig_before = sync.folder_signature(str(data))
    make_schedule_file(data / "schedule" / "ATC_AOC_Flight_Schedule.xlsx",
                       [("AMD", "DEL", "INDIGO", "6E1", "10:00", "MTWTFSS"),
                        ("AMD", "DEL", "INDIGO", "6E2", "18:00", "MTWTFSS")])
    os.utime(data / "schedule" / "ATC_AOC_Flight_Schedule.xlsx", (2_000_000_000, 2_000_000_000))
    shutil.copy(data / "rates" / "8.1 6E Eff. 16th Aug 2026.xlsx", data / "rates" / "9.0 6E Eff. 01st Sep 2026.xlsx")
    assert sync.folder_signature(str(data)) != sig_before
    third = sync.sync_data_folder(conn, str(data))
    assert third["schedule"].startswith("ATC_AOC_Flight_Schedule.xlsx (2 flights")
    assert third["rates_loaded"] == ["9.0 6E Eff. 01st Sep 2026.xlsx"]
    assert conn.execute("SELECT COUNT(*) FROM LoadBatches WHERE Status='OK'").fetchone()[0] == 2


def test_missing_data_folder_is_fine(conn, tmp_path):
    r = sync.sync_data_folder(conn, str(tmp_path / "nope"))
    assert r == {"schedule": None, "dgca": None, "history": [], "rates_loaded": [], "rates_skipped": 0, "errors": []}


def test_history_database_is_loaded_once(conn, tmp_path):
    import gzip

    from tariffapp import db

    # an "old CargoRates database" holding one IndiGo cycle
    old = tmp_path / "old.sqlite"
    oc = db.connect(str(old))
    db.init_schema(oc)
    oc.execute("INSERT INTO LoadBatches (Carrier, SourceFileName, EffectiveDate, Status) "
               "VALUES ('IndiGo', '7.0 6E Eff. 01st July 2026.xlsx', '2026-07-01 00:00:00', 'OK')")
    oc.execute("INSERT INTO IndiGo_GCR (BatchID, EffectiveDate, OriginCode, DestCode, RateN) "
               "VALUES (1, '2026-07-01 00:00:00', 'AMD', 'DEL', 70)")
    oc.commit()
    oc.close()
    data = tmp_path / "data"
    (data / "history").mkdir(parents=True)
    with open(old, "rb") as f, gzip.open(data / "history" / "CargoRates_history.sqlite.gz", "wb") as gz:
        gz.write(f.read())

    first = sync.sync_data_folder(conn, str(data))
    assert first["history"] == ["CargoRates_history.sqlite.gz (1 rate files)"] and not first["errors"]
    assert conn.execute("SELECT RateN FROM IndiGo_GCR WHERE OriginCode='AMD'").fetchone()[0] == 70
    again = sync.sync_data_folder(conn, str(data))
    assert again["history"] == []
    assert conn.execute("SELECT COUNT(*) FROM LoadBatches").fetchone()[0] == 1
