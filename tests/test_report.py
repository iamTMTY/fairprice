"""The report: wording, the data it is built from, its three file formats, and its draft-to-approved lifecycle."""

import csv
import io
import json
from unittest import mock

import pytest
from openpyxl import load_workbook

from conftest import ids
from fairprice import agent, db, report, server
from fairprice.storage import storage

HILUX = "SELECT id FROM lines WHERE description LIKE 'Supply of Toyota Hilux double cabin pickup unit%' ORDER BY id"
BASE = {"currency": "NGN", "year": 2025, "country": "NG"}


def state(item, price, candidates, challenge=None, searches=None, **extra):
    req = {**BASE, "item": item, "unit_price": price}
    return {"request": req, "final": server.check_price_comparability(price, "NGN", 2025, candidates, **extra),
            "challenge": challenge or {"verdict": "STANDS", "reason": "fine", "remove": {}},
            "searches": searches or [{"keywords": ["hilux"], "country": "NG", "matches": 11}]}


@pytest.fixture
def package():
    """Two lines, three bidders: one line with a band, one with too few records, and one bidder missing a line."""
    lines = [{"line": "1", "item": "Toyota Hilux double cabin pickup", "quantity": 2,
              "prices": {"A": 95e6, "B": 300e6, "C": 40e6}},
             {"line": "2", "item": "Laptop computer", "quantity": 10, "prices": {"A": 2e6, "B": 2.1e6}}]
    bundle = ids("SELECT id FROM lines WHERE description LIKE '%Prado%'")
    states = {"1": state("Toyota Hilux double cabin pickup", 95e6, ids(HILUX) + bundle,
                         rejected={str(bundle[0]): "Challenger: bundle"}),
              "2": state("Laptop computer", 2e6, ids("SELECT id FROM lines WHERE description LIKE 'Laptop computer%'"))}
    checks = [{"check": "arithmetic_error", "bidder": "B", "line": "1", "where": "sheet “B”, row 2",
               "data": {"quantity": 2, "unit_price": 300e6, "total": 700e6}},
              {"check": "missing_line", "bidder": "C", "line": "2", "where": None, "data": {"item": "Laptop computer"}}]
    return lines, states, ["A", "B", "C"], checks


def test_money_and_ranges():
    assert report.money(101990442, "NGN") == "₦101,990,442"
    assert [report.money(v, "NGN", True) for v in (1.25e6, 300e6, 2e9, 950)] == ["₦1.25M", "₦300M", "₦2B", "₦950"]
    assert report.money(5, "USD") == "USD 5" and report.money(None, "NGN") == ""
    assert report.ranges([5, 1, 2, 3]) == "1–3, 5" and report.ranges([]) == ""


def test_plain_reasons():
    assert report.plain_reason("Challenger: a bundle") == "Second check: a bundle"
    assert report.plain_reason("invalid_date:2001, multi_item_contract") == (
        "the date is missing or a placeholder (2001); the contract covers several different items, so it has no price per item")
    assert report.plain_reason("invalid_date:None") == "the date is missing or a placeholder"
    assert report.plain_reason("different model") == "different model"


def test_cells_judge_every_quote(package):
    lines, states, bidders, _ = package
    cells = report.cells(lines, {**states, "3": {"error": "boom"}}, bidders)
    assert cells[("1", "A")]["status"] == "within_band"
    assert cells[("1", "B")]["status"] == "above_band" and cells[("1", "C")]["status"] == "below_band"
    assert cells[("2", "A")]["status"] == "no_comparison" and cells[("2", "C")]["status"] == "not_priced"
    failed = report.cells([{"line": "3", "item": "x", "quantity": 1, "prices": {"A": 1}}], {"3": {"error": "boom"}}, ["A"])
    assert failed[("3", "A")]["status"] == "failed"


def test_check_sentences():
    where = {"bidder": "B", "line": "2", "where": "row 3"}
    assert "does not add up" in report.check_text({**where, "check": "arithmetic_error",
                                                   "data": {"quantity": 2, "unit_price": 5, "total": 11}}, "NGN")
    assert "asks for 2" in report.check_text({**where, "check": "quantity_changed", "data": {"quoted": 1, "requested": 2}}, "NGN")
    assert report.check_text({**where, "check": "missing_line", "data": {"item": "Laptop"}}, "NGN") == "B did not price line 2 (Laptop)."
    assert "A and C quoted exactly the same" in report.check_text(
        {"check": "identical_prices", "bidder": "A, C", "line": "1, 2", "where": None, "data": {"same": 2, "shared": 2}}, "NGN")
    assert report.check_text({**where, "check": "something_new", "data": {}}, "NGN") == "B, line 2 (row 3): something_new."


def test_terminal_summary(package):
    lines, states, bidders, checks = package
    text = report.summary(lines, states, bidders, checks, BASE)
    assert "Line 1 · Toyota Hilux double cabin pickup: typical past price" in text
    assert "Well above past prices (" in text and "Not priced" in text and "Problems in the bid files: 2" in text


def test_report_data_cites_its_records_and_never_ranks(package):
    lines, states, bidders, checks = package
    data = report.build("pkg.xlsx", lines, states, bidders, checks, BASE, "run1", "ollama:gemma4")
    assert data["report"]["status"] == "draft" and data["report"]["prepared_by"] == "FairPrice (ollama:gemma4)"
    assert [p["bidder"] for p in data["prices"][:3]] == bidders  # the file's order, not ranked
    assert data["prices"][0]["sources"] and data["prices"][2]["result"] == "Well below past prices"
    assert len(report.cited(data)) == len(data["sources"]) >= 8
    assert data["report"]["bid_file_checks"][1] == "C did not price line 2 (Laptop computer)."
    assert not any("rank" in k or "total" in k for k in data["prices"][0])
    single = report.build("x", *report.single(states["1"]), None, states["1"]["request"], "run2")
    assert single["report"]["bid_file_checks"] is None and single["prices"][0]["bidder"] == "Price under review"


def test_status_line_and_headings(package):
    lines, states, bidders, checks = package
    data = report.build("pkg", lines, states, bidders, checks, BASE, "run1")
    assert report.status_line(data) == report.DRAFT_STATUS
    data["report"].update(status="approved", approved_by="Ada", approved_at="2026-10-12T10:00:00+01:00")
    assert report.status_line(data) == "Approved by Ada on 12 October 2026 at 10:00"
    prices, srcs = report.headings(data)
    assert prices[0]["Quoted price per item (NGN)"] == 95000000 and isinstance(prices[0]["Quantity requested"], int)
    assert prices[0]["Sources (Ref numbers)"].startswith("1") and srcs[0]["FairPrice audit ID"].startswith("L")


def test_three_file_formats(package):
    lines, states, bidders, checks = package
    data = report.build("pkg", lines, states, bidders, checks, BASE, "run1")
    xlsx = report.files(data, "R", "xlsx")
    wb = load_workbook(io.BytesIO(xlsx["R.xlsx"]))
    assert wb.sheetnames == ["Prices", "Sources"] and wb["Prices"].oddHeader.center.text == report.DRAFT_STATUS
    assert wb["Prices"]["H3"].fill.fgColor.rgb.endswith("F8D7D7")  # Bidder B, well above: red
    assert wb["Prices"].cell(wb["Prices"].max_row, 1).value == report.DRAFT_STATUS
    out = report.files(data, "R", "csv")
    assert set(out) == {"R-prices.csv", "R-sources.csv"} and out["R-prices.csv"].startswith(b"\xef\xbb\xbf")
    rows = list(csv.DictReader(io.StringIO(out["R-prices.csv"].decode("utf-8-sig"))))
    assert rows[0]["Status"] == report.DRAFT_STATUS and rows[0]["Bidder"] == "A"
    assert json.loads(report.files(data, "R", "json")["R.json"]) == data
    empty = {**data, "sources": []}
    assert report.files(empty, "E", "csv")["E-sources.csv"].decode("utf-8-sig").startswith("Status")


def test_draft_approve_retrieve_and_reject(package, monkeypatch):
    lines, states, bidders, checks = package
    for fmt in report.FORMATS:
        monkeypatch.setenv("FAIRPRICE_REPORT_FORMAT", fmt)
        name = report.draft_name("Bid Package!", f"r{fmt}")
        assert name == f"DRAFT-bid-package-r{fmt}"
        agent.save_draft(f"r{fmt}", name, report.build("pkg", lines, states, bidders, checks, BASE, f"r{fmt}"), "summary")
        assert server.write_price_review(name)["status"] == "needs_approval"  # no person, no approval
        saved = server.write_price_review(name, db.grant("write_price_review", name, "Ada Obi"))
        assert saved["saved"].endswith(f"bid-package-r{fmt}") and saved["sources"] >= 8
        got = server.get_price_review(saved["saved"])
        assert got["report"]["approved_by"] == "Ada Obi" and got["files"]
        assert any(r["name"] == saved["saved"] for r in server.get_price_review()["reports"])
        with pytest.raises(Exception):
            storage().load(f"{name}.xlsx" if fmt == "xlsx" else f"{name}.json" if fmt == "json" else f"{name}-prices.csv")
    agent.save_draft("rj", "DRAFT-rejected-rj", report.build("pkg", lines, states, bidders, checks, BASE, "rj"), "")
    agent.discard_draft("rj", "DRAFT-rejected-rj")
    assert "not a draft" in server.write_price_review("DRAFT-rejected-rj", "x")["error"]
    assert "No approved report" in server.get_price_review("nothing")["error"]


def test_approval_refusals(package):
    lines, states, bidders, checks = package
    data = report.build("pkg", lines, states, bidders, checks, BASE, "rf")
    approve = lambda name: db.grant("write_price_review", name, "Ada")
    agent.save_draft("rf", "DRAFT-refusals-rf", data, "")
    assert "not an unused human approval" in server.write_price_review("DRAFT-refusals-rf", "made-up")["error"]
    other = approve("DRAFT-something-else")
    assert "not an unused human approval" in server.write_price_review("DRAFT-refusals-rf", other)["error"]
    first = server.write_price_review("DRAFT-refusals-rf")["request"]
    assert server.write_price_review("DRAFT-refusals-rf")["request"] == first  # the same request, not a new one
    no_sources = {**data, "sources": []}
    agent.save_draft("rf2", "DRAFT-nosources-rf2", no_sources, "")
    assert "cites no source records" in server.write_price_review("DRAFT-nosources-rf2", approve("DRAFT-nosources-rf2"))["error"]
    bogus = {**data, "sources": [{**data["sources"][0], "record_id": 999999999}]}
    agent.save_draft("rf3", "DRAFT-bogus-rf3", bogus, "")
    assert "do not exist: [999999999]" in server.write_price_review("DRAFT-bogus-rf3", approve("DRAFT-bogus-rf3"))["error"]


def test_opening_the_draft_only_when_asked(package, monkeypatch):
    lines, states, bidders, checks = package
    data = report.build("pkg", lines, states, bidders, checks, BASE, "op")
    with mock.patch("subprocess.run") as run:
        agent.save_draft("op", "DRAFT-open-op", data, "")
        run.assert_not_called()
        monkeypatch.setenv("FAIRPRICE_OPEN_DRAFT", "1")
        agent.save_draft("op", "DRAFT-open-op", data, "")
        run.assert_called_once()
    monkeypatch.setattr("sys.platform", "win32")
    with mock.patch("os.startfile", create=True) as start:
        agent.save_draft("op", "DRAFT-open-op", data, "")
    start.assert_called_once()
