"""Run evals/cases.json against the real agent, several times each, and record every result.

Usage: uv run fairprice-evals --repeats 3 [--model ollama:gemma4] [--cases hilux-above,no-benchmark]
Every run is stopped at the approval gate with "reject", which also tests that nothing is saved.
"""

import argparse
import asyncio
import contextlib
import io
import os
import tempfile
import datetime as dt
import json
import re
import statistics
import time
import uuid
from pathlib import Path
from unittest import mock

from langgraph.types import Command

from fairprice import agent, batch, db, report, server

ROOT = Path(__file__).resolve().parents[2]
CASES = ROOT / "evals" / "cases.json"
RESULTS = ROOT / "evals" / "results"
KNOWN_RECORD = 111893  # a real record id in the default data, for the checks that need one


def judge(case, state):
    final = state.get("final") or {}
    assessment = (final.get("assessment") or "unresolved").split(":")[0]
    accepted = [r["id"] for r in final.get("accepted", [])]
    failures = []
    if assessment not in case["expect"]:
        failures.append(f"assessment {assessment}, expected {'/'.join(case['expect'])}")
    if leaked := sorted(set(accepted) & set(case["traps"])):
        failures.append(f"trap records in band: {leaked}")
    if "max_accepted" in case and len(accepted) > case["max_accepted"]:
        failures.append(f"{len(accepted)} records accepted, expected at most {case['max_accepted']}")
    if state.get("saved"):
        failures.append("report saved although the reviewer rejected it")
    return {"assessment": assessment, "n": len(accepted), "median": (final.get("band") or {}).get("median"),
            "challenge": (state.get("challenge") or {}).get("verdict"), "failures": failures,
            "passed": not failures}


async def run_package(graph, case, model):
    """A whole bid package: per-bidder assessments from the shared bands, and nothing saved or ranked."""
    batch_id = f"eval-{case['id']}-{uuid.uuid4().hex[:6]}"
    base = {"currency": "NGN", "year": case["year"], "country": "NG"}
    agent.audit(batch_id, event="batch_started", sheet=case["package"], model=model)
    start = time.perf_counter()
    try:  # everything, including reading the package: one bad case must not stop the suite
        boq, bidders = batch.read_package(ROOT / case["package"])
        lines = batch.package_lines(boq, bidders)
        states = await batch.analyse(graph, lines, base, batch_id)
        got = {f"{line}/{b}": c["status"] for (line, b), c in report.cells(lines, states, list(bidders)).items()}
        data = report.build(case["package"], lines, states, list(bidders), batch.file_checks(boq, bidders), base, batch_id)
        table = [{**data["prices"][0], **report.headings(data)[0][0]}]  # field names and spreadsheet headings
        failures = [f"line {k}: {got.get(k)}, expected {'/'.join(v)}" for k, v in case["expect_cells"].items()
                    if got.get(k) not in v]
        failures += [f"ranking column {c!r}" for c in table[0] if re.search(r"rank|total|recommend|lowest", c, re.I)]
        result = {"assessment": " ".join(f"{k}={v}" for k, v in got.items()), "n": len(got), "median": None,
                  "challenge": None, "failures": failures, "passed": not failures}
    except Exception as e:
        result = {"assessment": "error", "n": 0, "median": None, "challenge": None,
                  "failures": [repr(e)[:300]], "passed": False}
    return {"case": case["id"], "run_id": batch_id, "seconds": round(time.perf_counter() - start, 1), **result}


async def run_case(graph, case, model):
    if "package" in case:
        return await run_package(graph, case, model)
    run_id = agent.CURRENT_RUN = f"eval-{case['id']}-{uuid.uuid4().hex[:6]}"
    config = {"configurable": {"thread_id": run_id}}
    req = {"item": case["item"], "unit_price": case["unit_price"], "year": case["year"],
           "currency": case.get("currency", "NGN"), "country": case.get("country", "NG")}
    agent.audit(run_id, event="run_started", request=req, model=model)
    start = time.perf_counter()
    try:
        await graph.ainvoke({"request": req}, config)
        await graph.ainvoke(Command(resume={"action": "reject"}), config)
        state = (await graph.aget_state(config)).values
        result = judge(case, state)
    except Exception as e:  # a crash is a failed run, not a reason to stop the suite
        result = {"assessment": "error", "n": 0, "median": None, "challenge": None,
                  "failures": [repr(e)[:300]], "passed": False}
    return {"case": case["id"], "run_id": run_id, "seconds": round(time.perf_counter() - start, 1), **result}


def fake_report(record_ids):
    """The smallest report that cites these record ids, for checks that need no model."""
    meta = {"title": "check", "status": "draft", "approved_by": None, "approved_at": None, "prepared_at": None,
            "prepared_by": "check", "run_id": "check", "currency": "NGN", "prices_in_year": 2025, "country": "NG",
            "bid_file_checks": None}
    prices = [{"line": "1", "item": "test item", "quantity_requested": None, "bidder": "Price under review",
               "quoted_price": 1.0, "typical_past_price": None, "times_typical": None,
               "result": "No comparison possible", "past_purchases_compared": len(record_ids),
               "sources": list(range(1, len(record_ids) + 1)), "problems": []}]
    srcs = [{"ref": n, "bought_by": "", "year": None, "what_was_bought": "", "quantity": None, "paid_per_item": None,
             "currency": "NGN", "adjusted_price": None, "published_by": None, "ocid": "", "contract_id": "",
             "dataset": "", "record_id": i} for n, i in enumerate(record_ids, 1)]
    return {"report": meta, "prices": prices, "sources": srcs}


@contextlib.contextmanager
def sandbox(provider="file", fmt="xlsx"):
    """A throwaway database and report store (local folder, or a mocked S3 bucket), so checks never touch real reports."""
    from moto import mock_aws

    tmp = tempfile.mkdtemp()
    env = {"FAIRPRICE_SQL_URL": f"sqlite:///{tmp}/check.sqlite", "FAIRPRICE_STORAGE": provider,
           "FAIRPRICE_REPORTS": tmp, "FAIRPRICE_REPORT_FORMAT": fmt, "FAIRPRICE_S3_BUCKET": "fairprice-test",
           "AWS_ACCESS_KEY_ID": "test", "AWS_SECRET_ACCESS_KEY": "test", "AWS_DEFAULT_REGION": "us-east-1"}
    with mock.patch.dict(os.environ, env), mock_aws(), contextlib.redirect_stdout(io.StringIO()):
        if provider == "s3":
            import boto3
            boto3.client("s3").create_bucket(Bucket="fairprice-test")
        yield


def round_trip(provider, fmt):
    """Draft -> approve -> list -> retrieve -> the files say who approved -> the draft files are gone."""
    from fairprice.storage import storage

    with sandbox(provider, fmt):
        agent.save_draft("check", "DRAFT-test-item-check", fake_report([KNOWN_RECORD]), "")
        draft_files = list(report.files(fake_report([KNOWN_RECORD]), "DRAFT-test-item-check", fmt))
        saved = server.write_price_review("DRAFT-test-item-check",
                                          db.grant("write_price_review", "DRAFT-test-item-check", "A. Officer"))
        listed = server.get_price_review()
        got = server.get_price_review(saved.get("saved", ""))
        texts = []
        for name in report.files(got, saved.get("saved", ""), fmt) if "saved" in saved else []:
            content = storage().load(name)
            if fmt == "xlsx":
                from openpyxl import load_workbook
                ws = load_workbook(io.BytesIO(content))["Prices"]
                content = str(ws.cell(ws.max_row, 1).value).encode()
            texts.append(content.decode("utf-8-sig"))
        drafts_left = []
        for name in draft_files:
            try:
                storage().load(name)
                drafts_left.append(name)
            except Exception:
                pass
    passed = ([r["name"] for r in listed.get("reports", [])] == [saved.get("saved")]
              and got.get("report", {}).get("approved_by") == "A. Officer" and texts and not drafts_left
              and all("A. Officer" in t for t in texts))
    return {"case": f"report ({fmt}, {provider} storage): draft approved, listed, retrieved as json, draft removed",
            "passed": bool(passed), "detail": {"saved": saved, "listed": listed, "drafts_left": drafts_left}}


def deterministic_checks():
    """Gate checks that need no model: the write tool needs a person's approval and refuses unsourced reports."""
    approve = lambda name: db.grant("write_price_review", name, "A. Officer")
    with sandbox():
        for name, ids in (("DRAFT-a", [KNOWN_RECORD]), ("DRAFT-b", []), ("DRAFT-c", [999999999]),
                          ("DRAFT-d", [KNOWN_RECORD])):
            agent.save_draft("check", name, fake_report(ids), "")
        asked = server.write_price_review("DRAFT-a")
        checks = {
            "write refused with an approval for a different report": server.write_price_review("DRAFT-a", approve("DRAFT-d")),
            "write refused for a report that cites no records": server.write_price_review("DRAFT-b", approve("DRAFT-b")),
            "write refused for a report citing a record that does not exist":
                server.write_price_review("DRAFT-c", approve("DRAFT-c")),
            "write refused for a name that is not a draft": server.write_price_review("../pyproject.toml", "x"),
        }
        once = approve("DRAFT-d")
        first, second = db.use_approval(once, "write_price_review", "DRAFT-d"), db.use_approval(once, "write_price_review", "DRAFT-d")
        agent.discard_draft("check", "DRAFT-a")
        discarded = server.write_price_review("DRAFT-a", approve("DRAFT-a"))
    results = [{"case": "write without a person's approval only records a request", "detail": asked,
                "passed": asked.get("status") == "needs_approval" and "fairprice --approve" in asked.get("message", "")}]
    results += [{"case": name, "passed": "error" in out, "detail": out} for name, out in checks.items()]
    results.append({"case": "a person's approval can be used only once", "passed": bool(first) and second is None,
                    "detail": None})
    results.append({"case": "a rejected draft is deleted and can no longer be approved", "passed": "error" in discarded,
                    "detail": discarded})
    results += [round_trip("file", fmt) for fmt in report.FORMATS] + [round_trip("s3", "xlsx")]
    packages = [batch.load(ROOT / "examples" / f"bid-package.{ext}")[0] for ext in ("xlsx", "csv", "json")]
    results.append({"case": "the xlsx, csv and json templates read to the same bid package",
                    "passed": packages[0] == packages[1] == packages[2], "detail": None})
    bad = {"unknown column": "bidder,line,item,quantity,rate\nBoQ,1,Laptop,2,\nBidder A,1,,2,500\n",
           "price in words": "bidder,line,item,quantity,unit_price\nBoQ,1,Laptop,2,\nBidder A,1,,2,two million\n",
           "line not in the BoQ": "bidder,line,item,quantity,unit_price\nBoQ,1,Laptop,2,\nBidder A,9,,2,500\n",
           "no BoQ": "bidder,line,item,quantity,unit_price\nBidder A,1,,2,500\n"}
    for name, text in bad.items():
        path = Path(tempfile.mkdtemp()) / "package.csv"
        path.write_text(text)
        try:
            batch.load(path)
            refused = None
        except batch.PackageError as e:
            refused = e.problems
        results.append({"case": f"a bid package with {name} is refused, with its location", "passed": bool(refused),
                        "detail": refused})
    boq, bidders = batch.read_package(ROOT / "examples" / "bid-package.xlsx")
    found = {(c["check"], c["bidder"], c["line"]) for c in batch.file_checks(boq, bidders)}
    planted = {("arithmetic_error", "Bidder B", "2"), ("quantity_changed", "Bidder C", "1"),
               ("missing_line", "Bidder C", "3"), ("identical_prices", "Bidder A, Bidder C", "1, 2")}
    results.append({"case": "bid file checks find the 4 planted problems, and only those",
                    "passed": found == planted, "detail": sorted(found ^ planted)})
    return results


async def main_async(args):
    cases = json.loads(CASES.read_text())
    if args.cases:
        cases = [c for c in cases if c["id"] in args.cases.split(",")]
    RESULTS.mkdir(parents=True, exist_ok=True)
    out = RESULTS / f"{dt.datetime.now():%Y%m%d-%H%M}-{args.model.replace(':', '_').replace('/', '_')}.jsonl"
    results = []
    async with agent.session(args.model) as (graph, _):
        for case in cases:
            for i in range(args.repeats):
                r = await run_case(graph, case, args.model)
                results.append(r)
                with out.open("a") as f:
                    f.write(json.dumps(r) + "\n")
                print(f"{case['id']:<22} run {i + 1}: {'PASS' if r['passed'] else 'FAIL'} "
                      f"{r['assessment']:<26} n={r['n']:<3} {r['seconds']:>6}s  {'; '.join(r['failures'])}", flush=True)
    det = deterministic_checks()
    print("\nSummary", args.model)
    for case in cases:
        rs = [r for r in results if r["case"] == case["id"]]
        medians = [r["median"] for r in rs if r["median"]]
        spread = f"median range {min(medians):,.0f}-{max(medians):,.0f}" if len(medians) > 1 else ""
        print(f"  {case['id']:<22} {sum(r['passed'] for r in rs)}/{len(rs)} passed  "
              f"avg {statistics.mean(r['seconds'] for r in rs):.0f}s  {spread}")
    for d in det:
        print(f"  {d['case']:<60} {'PASS' if d['passed'] else 'FAIL'}")
    print(f"\nRaw results: {out}")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--repeats", type=int, default=3)
    p.add_argument("--model", default=agent.MODEL)
    p.add_argument("--cases", help="comma-separated case ids")
    asyncio.run(main_async(p.parse_args()))


if __name__ == "__main__":
    main()
