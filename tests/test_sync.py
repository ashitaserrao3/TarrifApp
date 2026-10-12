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
    assert r == {"schedule": None, "rates_loaded": [], "rates_skipped": 0, "errors": []}
