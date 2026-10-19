"""A tariff file holding only changed lines updates those lines and keeps the rest."""

import datetime as dt

from conftest import make_indigo_file

from tariffapp.etl.loader import load_rate_file
from tariffapp.linker import TariffEngine


def flight(o, d, no, dep="10:00"):
    return {"Origin": o, "Destination": d, "Airline": "INDIGO", "FlightNo": no, "DepTime": dep,
            "Mon": 1, "Tue": 1, "Wed": 1, "Thu": 1, "Fri": 1, "Sat": 1, "Sun": 1}


def test_newer_file_replaces_only_its_lines(conn, tmp_path):
    full = tmp_path / "Indigo All India Tariff Rate List Eff. 01st Oct 2026.xlsx"
    make_indigo_file(full, [("AMD", "DEL", "GCR", 1800, 70, 50, 40, None, None, None),
                            ("AMD", "BOM", "GCR", 1800, 60, 45, 35, None, None, None)])
    update = tmp_path / "Indigo Revised Rates Eff. 16th Oct 2026.xlsx"
    make_indigo_file(update, [("AMD", "DEL", "GCR", 1800, 75, 55, 44, None, None, None)])
    for f in (full, update):
        assert load_rate_file(conn, str(f))["status"] == "ok"

    eng = TariffEngine(conn, dt.date(2026, 10, 20))
    assert eng.price(flight("AMD", "DEL", "6E1")).rate_at(100)[0] == 44  # the changed line
    assert eng.price(flight("AMD", "BOM", "6E2")).rate_at(100)[0] == 35  # kept from the full file

    before = TariffEngine(conn, dt.date(2026, 10, 10))  # the update isn't in force yet
    assert before.price(flight("AMD", "DEL", "6E1")).rate_at(100)[0] == 40


def test_change_report_lists_changed_new_and_missing_lines(tmp_path):
    import tariff_changes as T
    old = tmp_path / "Indigo All India Tariff Rate List Eff. 01st Oct 2026.xlsx"
    make_indigo_file(old, [("AMD", "DEL", "GCR", 1800, 70, 50, 40, None, None, None),
                           ("AMD", "BOM", "GCR", 1800, 60, 45, 35, None, None, None),
                           ("AMD", "GOI", "GCR", 1800, 60, 45, 35, None, None, None)])
    new = tmp_path / "Indigo All India Tariff Rate List Eff. 16th Oct 2026.xlsx"
    make_indigo_file(new, [("AMD", "DEL", "GCR", 1800, 70, 52, 41, None, None, None),
                           ("AMD", "BOM", "GCR", 1800, 60, 45, 35, None, None, None),
                           ("AMD", "PNQ", "GCR", 1800, 66, 50, 44, None, None, None)])
    carrier, eff_old, before = T.read_tariff(str(old))
    _, eff_new, after = T.read_tariff(str(new))
    assert carrier == "IndiGo" and (eff_old.day, eff_new.day) == (1, 16)
    r = T.compare(T.current_lines([(eff_old, before)]), after)
    assert [(T.describe(k), d) for k, _, d in r["changed"]] == [("AMD→DEL · GCR", [("45+", 50, 52), ("100+", 40, 41)])]
    assert [T.describe(k) for k, _ in r["added"]] == ["AMD→PNQ · GCR"]
    assert [T.describe(k) for k, _ in r["missing"]] == ["AMD→GOI · GCR"]
    assert r["same"] == 1
