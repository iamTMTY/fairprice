"""The fairprice MCP server's tools, called directly (they are plain functions under the decorators)."""

import json
import os
from unittest import mock

from conftest import ids, offline_registry
from fairprice import db, server

HILUX = "SELECT id FROM lines WHERE description LIKE 'Supply of Toyota Hilux double cabin pickup unit%' ORDER BY id"


def by_desc(text):
    return ids("SELECT id FROM lines WHERE description = ?", text)[0]


def test_find_comparables_searches_with_every_keyword_and_flags_records():
    out = server.find_comparables(["hilux"], country="NG")
    assert out["total_matches"] == 11 and out["returned"] == 11
    flags = {r["description"]: r["flags"] for r in out["results"]}
    assert flags["Toyota Hilux double cabin pickup (placeholder date)"] == ["invalid_date:2001"]
    assert flags["Toyota Hilux double cabin pickup for testing"] == ["test_record"]
    assert "line_type" not in out["results"][0] and "n_items" not in out["results"][0]
    assert server.find_comparables(["hilux", "prado"])["total_matches"] == 1
    assert server.find_comparables(["hilux"], year_from=2023, year_to=2023, limit=1)["returned"] == 1
    assert server.find_comparables([]) == {"error": "Give at least one keyword."}


def test_awards_without_quantities_are_flagged_as_such():
    out = server.find_comparables(["hilux"], country="KE")
    assert {tuple(r["flags"]) for r in out["results"]} == {("no_quantity_published",)}
    multi = server.data_flags({"year": 2023, "n_items": 2, "unit_price": None, "country": "NG", "buyer": "X"})
    assert multi == ["multi_item_contract", "no_unit_price"]
    assert server.data_flags({"year": None, "n_items": 1, "unit_price": 1, "country": "NG", "buyer": None}) == ["invalid_date:None"]


def test_record_ids_and_assessment_rules():
    assert server.record_id("L113240") == server.record_id("113240") == server.record_id(113240) == 113240
    assert server.assess(150, 100) == (1.5, "above_band: 1.50x the median")
    assert server.assess(60, 100) == (0.6, "below_band: 0.60x the median")
    assert server.assess(100, 100) == (1.0, "within_band")
    assert server.index_value("cpi", "NG", 2030)[1] == 2025  # latest year published
    assert server.index_value("cpi", "XX", 2025) == (None, None)


def test_price_band_excludes_flagged_rejected_and_duplicate_records():
    good, everything = ids(HILUX), ids("SELECT id FROM lines WHERE country = 'NG' AND description LIKE '%Hilux%'")
    bundle = by_desc("Supply of 2 Toyota Hilux and 1 Toyota Prado")
    out = server.check_price_comparability(95e6, "NGN", 2025, [i for i in everything if i != bundle] + [999999999],
                                           rejected={f"L{bundle}": "bundle with a Prado"})
    reasons = {e["id"]: e["reason"] for e in out["excluded"]}
    assert reasons[bundle] == "bundle with a Prado"
    assert reasons[by_desc("Toyota Hilux double cabin pickup for testing")] == "test_record"
    assert "duplicate of an earlier record" in reasons.values()
    assert out["unknown_ids"] == [999999999]
    assert out["band"]["n"] == len(good) - 1 == 7
    assert out["assessment"] == "within_band"
    assert out["accepted"][0]["adjusted_unit_price"] > out["accepted"][0]["published_unit_price"]  # inflation to 2025


def test_price_far_above_or_below_the_band():
    good = ids(HILUX)
    assert server.check_price_comparability(400e6, "NGN", 2025, good)["assessment"].startswith("above_band")
    assert server.check_price_comparability(10e6, "NGN", 2025, good)["assessment"].startswith("below_band")


def test_too_few_comparables_gives_no_band():
    laptops = ids("SELECT id FROM lines WHERE description LIKE 'Laptop computer%'")
    out = server.check_price_comparability(2e6, "NGN", 2025, laptops)
    assert out["band"] is None and out["assessment"].startswith("insufficient_comparables: 2 accepted")
    assert server.check_price_comparability(1, "NGN", 2025, []) == {"error": "No candidate_ids given."}


def test_currency_conversion_and_missing_years():
    good = ids(HILUX)
    in_kes = server.check_price_comparability(30e6, "KES", 2026, good)
    assert in_kes["accepted"][0]["adjusted_unit_price"] < in_kes["accepted"][0]["published_unit_price"]
    assert "CPI for 2026 not published; used 2025." in in_kes["notes"]
    no_rate = server.check_price_comparability(1000, "USD", 2025, good)
    assert not no_rate["accepted"] and "cannot convert NGN to USD" in {e["reason"] for e in no_rate["excluded"]}


def test_source_record_returns_the_published_document():
    record = json.loads(server.source_record(str(ids(HILUX)[0])))
    assert record["doc"]["ocid"].startswith("ocds-tstng1") and record["source"].endswith("/64")
    assert json.loads(server.source_record("999999999")) == {"error": "no record L999999999"}


def test_list_countries_and_add_country():
    with offline_registry():
        listed = server.list_countries()
        codes = {c["code"]: c for c in listed["countries"]}
        assert codes["NG"]["loaded"] and codes["RW"]["loaded"] and codes["NG"]["publications"] == 2
        assert server.add_country("zz")["error"].startswith("No data on the OCP Data Registry")
        proc = mock.Mock(pid=os.getpid())  # a process that is running
        with mock.patch("subprocess.Popen", return_value=proc) as popen, \
                mock.patch.object(server, "ROOT", server.Path(os.environ["FAIRPRICE_REPORTS"]).parent):
            asked = server.add_country("rw")
            assert asked["status"] == "needs_approval" and "fairprice --approve" in asked["message"]
            popen.assert_not_called()  # nothing happens without a person
            assert "not an unused human approval" in server.add_country("rw", "made-up")["error"]
            started = server.add_country("rw", db.grant("add_country", "RW", "Ada"))
            assert started["started"] == "RW" and popen.call_args.args[0][-2:] == ["--add", "RW"]
            assert "already running" in server.add_country("ng")["error"]
        from fairprice import ingest
        ingest.status(state="running", pid=None)  # an ingest that died without finishing
        assert server.list_countries()["ingest"]["state"] == "stopped"
        ingest.status(state="done", pid=None)


def test_running_checks_a_process_exists():
    assert server.running(os.getpid()) is True
    assert server.running(None) is False


def test_main_starts_the_server():
    with mock.patch.object(server.mcp, "run") as run:
        server.main()
    run.assert_called_once()
