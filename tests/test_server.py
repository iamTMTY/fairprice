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
    out = server.search(["hilux"], country="NG")
    assert out["total_matches"] == 11 and out["returned"] == 11
    flags = {r["description"]: r["flags"] for r in out["results"]}
    assert flags["Toyota Hilux double cabin pickup (placeholder date)"] == ["invalid_date:2001"]
    assert flags["Toyota Hilux double cabin pickup for testing"] == ["test_record"]
    assert "line_type" not in out["results"][0] and "n_items" not in out["results"][0]
    assert server.search(["hilux", "prado"])["total_matches"] == 1
    assert server.search(["hilux"], year_from=2023, year_to=2023, limit=1)["returned"] == 1
    assert server.search([]) == {"error": "Give at least one keyword."}
    # every word, in any order, however the keywords are split
    assert server.search(["pickup hilux"], "NG") == server.search(["hilux", "pickup"], "NG") != server.search(["hilux pickup unit"], "NG")
    assert server.search(["hilux"], "NG", limit=500)["returned"] == 11  # limit is clamped to 50, not an error


def test_awards_without_quantities_are_flagged_as_such():
    out = server.search(["hilux"], country="KE")
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
    out = server.price_band(95e6, "NGN", 2025, [i for i in everything if i != bundle] + [999999999],
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
    assert server.price_band(400e6, "NGN", 2025, good)["assessment"].startswith("above_band")
    assert server.price_band(10e6, "NGN", 2025, good)["assessment"].startswith("below_band")


def test_too_few_comparables_gives_no_band():
    laptops = ids("SELECT id FROM lines WHERE description LIKE 'Laptop computer%'")
    out = server.price_band(2e6, "NGN", 2025, laptops)
    assert out["band"] is None and out["assessment"].startswith("insufficient_comparables: 2 accepted")
    assert server.price_band(1, "NGN", 2025, []) == {"error": "No candidate_ids given."}


def test_currency_conversion_and_missing_years():
    good = ids(HILUX)
    in_kes = server.price_band(30e6, "KES", 2026, good)
    assert in_kes["accepted"][0]["adjusted_unit_price"] < in_kes["accepted"][0]["published_unit_price"]
    assert "CPI for 2026 not published; used 2025." in in_kes["notes"]
    no_rate = server.price_band(1000, "USD", 2025, good)
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


def test_a_review_is_one_run_across_tool_calls():
    started = server.start_review("Toyota Hilux double cabin pickup", 95e6, "ngn", 2025, "ng")
    run = started["run_id"]
    assert started["currency"] == "NGN" and started["country"] == "NG" and started["lines"][0]["line"] == "1"
    assert "error" in server.start_review(" ", 95e6) and "error" in server.start_review("x", 0)
    assert server.plan_searches(run, [["hilux"], [], ["pickup"]], "reject bundles")["plan"] == [["hilux"], ["pickup"]]
    assert "error" in server.plan_searches(run, [[]])
    found = server.find_comparables(run, ["hilux"])
    assert found["total_matches"] == 11  # only the review's own country: Kenya's Hilux awards are not searched
    good, bundle = ids(HILUX), by_desc("Supply of 2 Toyota Hilux and 1 Toyota Prado")
    kenyan = ids("SELECT id FROM lines WHERE country = 'KE'")[0]
    refused = server.check_price_comparability(run, [kenyan])
    assert refused["error"].startswith("Refused: none of the candidate") and str(kenyan) in refused["error"]
    mixed = server.check_price_comparability(run, good + [kenyan], {str(kenyan): "x"})
    assert mixed["left_out"]["ids"] == [kenyan] and kenyan not in [r["id"] for r in mixed["accepted"] + mixed["excluded"]]
    band = server.check_price_comparability(run, good, {f"L{bundle}": "bundle"})
    assert band["assessment"] == "within_band" and band["price_under_review"] == 95e6 and band["year"] == 2025
    assert "error" in server.challenge_review(run, "MAYBE", "x")
    assert "error" in server.challenge_review(run, "DISPUTED", "x", {"999999999": "not in the band"})
    assert server.challenge_review(run, "disputed", "mixes years", {str(good[0]): "old"})["recorded"]
    _, lines, states = server.review(run)
    state = states["1"]
    assert state["final"] == band and state["plan"]["searches"] == [["hilux"], ["pickup"]]
    assert state["searches"] == [{"keywords": ["hilux"], "country": "NG", "matches": 11}]
    assert state["challenge"]["verdict"] == "DISPUTED" and state["challenge"]["applied"] is False
    again = server.check_price_comparability(run, good, {str(good[0]): "Challenger: old", str(bundle): "bundle"})
    assert server.review(run)[2]["1"]["challenge"]["applied"] is True and server.review(run)[2]["1"]["final"] == again
    for call in (lambda: server.find_comparables("nope", ["hilux"]), lambda: server.plan_searches(run, [["x"]], line="9"),
                 lambda: server.get_price_review("nope")):
        assert "error" in call()


def test_challenge_needs_a_band_and_check_needs_a_price():
    run = server.start_review("Laptop computer core i5", 2e6, year=2025)["run_id"]
    assert "error" in server.challenge_review(run, "STANDS", "fine")  # nothing checked yet
    server.find_comparables(run, ["laptop computer"])
    out = server.check_price_comparability(run, ids("SELECT id FROM lines WHERE description LIKE 'Laptop computer%'"))
    assert out["band"] is None and "error" in server.challenge_review(run, "STANDS", "fine")


def test_a_package_review_is_one_run_with_a_line_per_boq_line():
    package = {"boq": [{"line": "1", "item": "Toyota Hilux double cabin pickup", "quantity": 2},
                       {"line": "2", "item": "Laptop computer", "quantity": 5}],
               "bids": [{"bidder": "Bidder A", "lines": [{"line": "1", "unit_price": 95e6, "quantity": 2, "total": 190e6}]},
                        {"bidder": "Bidder B", "lines": [{"line": "1", "unit_price": 99e6, "quantity": 2, "total": 198e6}]}]}
    started = server.start_package_review(package, "tender 7", year=2025)
    run = started["run_id"]
    assert [entry["line"] for entry in started["lines"]] == ["1", "2"]
    assert "error" in server.check_price_comparability(run, [1], line="2")  # nobody priced line 2
    server.find_comparables(run, ["hilux"])
    band = server.check_price_comparability(run, ids(HILUX))
    assert band["price_under_review"] == 97e6  # the median of the bidders' prices
    assert "error" in server.start_package_review({"boq": []})


def test_the_currency_defaults_to_the_countrys():
    assert server.country_currency("ke") == "KES" and server.country_currency("ZZ") is None
    assert server.start_review("Laptop computer", 2e6, country="KE")["currency"] == "KES"
    assert server.start_review("Laptop computer", 2e6, "usd", country="NG")["currency"] == "USD"  # unless given
    assert "is not loaded" in server.start_review("Laptop computer", 2e6, country="ZZ")["error"]
    assert "is not loaded" in server.start_package_review({"boq": [{"line": "1", "item": "x", "quantity": 1}],
                                                           "bids": [{"bidder": "A", "lines": [{"line": "1", "unit_price": 1}]}]},
                                                          country="ZZ")["error"]
