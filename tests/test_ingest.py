"""Ingest: registry parsing, one file per country, the manifest, the three readings, stable record ids, statuses."""

import json

import duckdb
import pytest

from conftest import ids, offline_registry
from fairprice import ingest


def query(sql, *params):
    with duckdb.connect(str(ingest.DB), read_only=True) as con:
        return con.execute(sql, params).fetchall()


def test_one_file_per_country_and_a_manifest():
    assert sorted(p.name for p in ingest.RAW.glob("*.jsonl.gz")) == ["kenya.jsonl.gz", "nigeria.jsonl.gz", "rwanda.jsonl.gz"]
    manifest = json.loads(ingest.MANIFEST.read_text())
    assert [p["publication"] for p in manifest] == [64, 147, 145]  # in the order added; the empty entry skipped
    assert {p["prefix"] for p in manifest} == {"ocds-tstke1", "ocds-tstng1", "ocds-tstrw1"}


def test_each_publication_is_read_the_right_way():
    rows = dict((r[0], r[1:]) for r in query("SELECT publication, currency, priced_per, first_id, last_id FROM sources"))
    assert rows[147][:2] == ("KES", "awards")
    assert rows[64][:2] == ("NGN", "items")
    assert rows[145][:2] == ("RWF", "item_prices")
    assert rows[64][2] == 1 and rows[147][2] == rows[64][3] + 1 and rows[145][2] == rows[147][3] + 1  # contiguous blocks


def test_lines_carry_unit_prices_only_where_the_data_supports_them():
    assert query("SELECT count(*), count(unit_price) FROM lines WHERE country = 'KE'") == [(3, 0)]
    assert query("SELECT count(unit_price) FROM lines WHERE country = 'RW'") == [(3,)]
    toner = query("SELECT description, quantity, unit_price FROM lines WHERE country = 'RW' ORDER BY unit_price")
    assert toner[0] == ("Mouse USB (Printer or facsimile toner)", 20.0, 4000.0)
    assert ids("SELECT id FROM lines WHERE description = 'Laptops and printers' AND unit_price IS NULL")


def test_cpi_and_exchange_rates_for_every_loaded_country():
    assert {r[0] for r in query("SELECT DISTINCT country FROM cpi")} == {"KE", "NG", "RW"}
    assert {r[0] for r in query("SELECT DISTINCT country FROM fx")} == {"KE", "NG", "RW"}


def test_rebuild_keeps_record_ids(capsys):
    before = query("SELECT id, ocid, line_id FROM lines ORDER BY id")
    with offline_registry():
        ingest.main(["--rebuild"])  # from the files already downloaded
    assert query("SELECT id, ocid, line_id FROM lines ORDER BY id") == before
    assert ingest.status()["state"] == "done"


def test_list_marks_loaded_countries(capsys):
    with offline_registry():
        ingest.main(["--list"])
    out = capsys.readouterr().out
    assert "NG   Nigeria" in out and "(loaded)" in out and "2 publication(s)" in out


def test_adding_an_unknown_or_loaded_country():
    with offline_registry():
        with pytest.raises(SystemExit, match="No data on the registry for ZZ"):
            ingest.main(["--add", "ZZ"])
        assert ingest.status()["state"] == "failed"
        ingest.main(["--add", "RW"])  # nothing new: just a rebuild
    assert ingest.status()["state"] == "done"


def test_helpers():
    assert ingest.file_for("Côte d'Ivoire") == "cote-d-ivoire.jsonl.gz"
    assert ingest.countries([{"country": "NG", "title": "Nigeria: A", "publication": 1},
                             {"country": "NG", "title": "Nigeria: B", "publication": 2}])["NG"]["publications"] == 2
    with pytest.raises(SystemExit, match="No data downloaded yet"):
        ingest.build([])


def test_registry_layout_change_is_reported():
    from unittest import mock
    with mock.patch.object(ingest, "fetch", lambda url: "<html>no articles</html>"):
        with pytest.raises(SystemExit, match="layout may have changed"):
            ingest.registry()


def test_legacy_files_without_a_manifest(tmp_path, monkeypatch):
    monkeypatch.setattr(ingest, "RAW", tmp_path)
    monkeypatch.setattr(ingest, "MANIFEST", tmp_path / "manifest.json")
    (tmp_path / "nigeria.jsonl.gz").write_bytes(b"")
    assert [p["publication"] for p in ingest.manifest()] == [64]


def test_a_failed_append_leaves_the_country_file_untouched(tmp_path, monkeypatch):
    from unittest import mock
    monkeypatch.setattr(ingest, "RAW", tmp_path)
    target = tmp_path / "rwanda.jsonl.gz"
    target.write_bytes(b"existing")
    with offline_registry(), mock.patch("pathlib.Path.read_bytes", side_effect=OSError("disk full")):
        with pytest.raises(OSError):
            ingest.append({"publication": 145, "title": "Rwanda"}, "rwanda.jsonl.gz")
    assert target.stat().st_size == len(b"existing")
    assert not list(tmp_path.glob("*.part"))


def test_a_country_code_is_always_needed(monkeypatch, capsys):
    with pytest.raises(SystemExit):
        ingest.main([])
    assert "give a country code to load, e.g. --add NG" in capsys.readouterr().err
    monkeypatch.setattr(ingest, "manifest", lambda: [])
    with pytest.raises(SystemExit, match="Nothing downloaded yet"):
        ingest.main(["--rebuild"])
