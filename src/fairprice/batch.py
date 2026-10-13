"""Review a bid package: every bidder's price schedule for one procurement, line by line.

Usage:
  uv run fairprice --package examples/bid-package.xlsx --year 2025     (or .csv, or .json)

A package must match bid_package.schema.json exactly; one that does not is refused with every problem and where it
is, never interpreted. The three forms of the same package:
  xlsx  a sheet named "BoQ" (columns line, item, quantity) and one sheet per bidder, named after the bidder
        (columns line, unit_price, and optionally quantity, total, item)
  csv   one file with columns bidder, line, item, quantity, unit_price, total; rows whose bidder is "BoQ" are the BoQ
  json  {"boq": [{line, item, quantity}], "bids": [{bidder, lines: [{line, unit_price, quantity, total, item}]}]}
The agent builds one price band per distinct item, then code compares every bidder's price with it and checks the
bid files. Bidders are never totalled, ranked or recommended.
"""

import csv
import json
import re
import statistics
import uuid
from itertools import combinations
from pathlib import Path

import jsonschema
from openpyxl import load_workbook

from fairprice import agent, report
from fairprice.db import grant

MIN_IDENTICAL = 2  # ponytail: fixed threshold for the identical-prices pattern; tune with committee feedback
SCHEMA = json.loads(Path(__file__).with_name("bid_package.schema.json").read_text())
BOQ_COLUMNS = ("line", "item", "quantity")
BID_COLUMNS = ("line", "unit_price", "quantity", "total", "item")
CSV_COLUMNS = ("bidder", "line", "item", "quantity", "unit_price", "total")
NUMERIC = {"quantity", "unit_price", "total"}
NUMBER = re.compile(r"^-?(\d+|\d{1,3}(,\d{3})+)(\.\d+)?$")  # 2500000, 2,500,000 or 2500000.50; nothing else


class PackageError(ValueError):
    """A bid package that does not match the schema. Lists every problem and where it is."""

    def __init__(self, path, problems):
        self.problems = problems
        super().__init__(f"{Path(path).name} was not read: it does not match the bid package format "
                         f"(bid_package.schema.json).\n" + "\n".join(f"  - {p}" for p in problems))


def cell(value, where, problems, numeric):
    """A cell as written: text, or a number. Anything that is not plainly a number is a problem, not a guess."""
    if value is None or str(value).strip() == "":
        return None
    if not numeric:
        return str(int(value)) if isinstance(value, float) and value.is_integer() else str(value).strip()
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if NUMBER.match(text := str(value).strip()):
        return float(text.replace(",", ""))
    problems.append(f"{where}: {text!r} is not a number")
    return None


def records(rows, allowed, required, label, problems):
    """[(where, {column: value})] from a table whose first row holds exactly the allowed column names."""
    rows = [list(r) for r in rows if any(c not in (None, "") for c in r)]
    if not rows:
        problems.append(f"{label}: no rows")
        return []
    header = [str(c).strip().lower() if c not in (None, "") else "" for c in rows[0]]
    if unknown := [h for h in header if h and h not in allowed]:
        problems.append(f"{label}, row 1: unknown column(s) {', '.join(unknown)}; the columns are {', '.join(allowed)}")
    if missing := [c for c in required if c not in header]:
        problems.append(f"{label}, row 1: missing column(s) {', '.join(missing)}")
    if unknown or missing:
        return []
    out = []
    for n, r in enumerate(rows[1:], start=2):
        where = f"{label}, row {n}"
        if any(v not in (None, "") for h, v in zip(header, r) if not h) or len(r) > len(header):
            problems.append(f"{where}: a value in a column with no name")
        values = {h: cell(v, f"{where}, {h}", problems, h in NUMERIC) for h, v in zip(header, r) if h}
        out.append((where, {k: v for k, v in values.items() if v is not None}))
    return out


def read_xlsx(path, problems):
    wb = load_workbook(path, read_only=True, data_only=True)
    package, where = {"boq": [], "bids": []}, {}
    sheets = {ws.title: list(ws.iter_rows(values_only=True)) for ws in wb.worksheets}
    if not any(name.strip().lower() == "boq" for name in sheets):
        problems.append('no sheet named "BoQ" (the bill of quantities)')
    for name, rows in sheets.items():
        label = f"sheet “{name}”"
        if name.strip().lower() == "boq":
            for w, r in records(rows, BOQ_COLUMNS, BOQ_COLUMNS, label, problems):
                where[("boq", len(package["boq"]))] = w
                package["boq"].append(r)
        else:
            bid = {"bidder": name.strip(), "lines": []}
            for w, r in records(rows, BID_COLUMNS, ("line", "unit_price"), label, problems):
                where[("bids", len(package["bids"]), "lines", len(bid["lines"]))] = w
                bid["lines"].append(r)
            package["bids"].append(bid)
    return package, where


def read_csv(path, problems):
    with open(path, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.reader(f))
    package, where, bids = {"boq": [], "bids": []}, {}, {}
    for w, r in records(rows, CSV_COLUMNS, ("bidder", "line"), Path(path).name, problems):
        bidder = r.pop("bidder", None)
        if bidder is None:
            problems.append(f"{w}: no bidder (write BoQ for bill-of-quantities rows)")
        elif bidder.lower() == "boq":
            if extra := sorted(set(r) - set(BOQ_COLUMNS)):
                problems.append(f"{w}: a BoQ row has no {', '.join(extra)}")
            where[("boq", len(package["boq"]))] = w
            package["boq"].append({k: v for k, v in r.items() if k in BOQ_COLUMNS})
        else:
            if bidder not in bids:
                bids[bidder] = {"bidder": bidder, "lines": []}
                package["bids"].append(bids[bidder])
            j = package["bids"].index(bids[bidder])
            where[("bids", j, "lines", len(bids[bidder]["lines"]))] = w
            bids[bidder]["lines"].append(r)
    if not package["boq"] and not problems:
        problems.append("no BoQ rows (rows whose bidder is BoQ)")
    return package, where


def read_json(path, problems):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8-sig")), {}
    except json.JSONDecodeError as e:
        problems.append(f"not valid JSON: {e}")
        return {}, {}


def locate(path, where):
    """The spreadsheet location of a schema error, or its place in the JSON (bids[1].lines[0].unit_price)."""
    for n in range(len(path), 0, -1):
        if tuple(path[:n]) in where:
            return where[tuple(path[:n])] + "".join(f", {p}" for p in path[n:])
    return "".join(f"[{p}]" if isinstance(p, int) else f".{p}" for p in path).lstrip(".") or "the package"


def load(path):
    """Read a bid package (.xlsx, .csv or .json) into the schema's form, or raise PackageError with every problem.
    Returns (package, where): where maps each row to its location in the file, for findings to cite."""
    readers = {".xlsx": read_xlsx, ".csv": read_csv, ".json": read_json}
    if Path(path).suffix.lower() not in readers:
        raise PackageError(path, [f"use .xlsx, .csv or .json, not {Path(path).suffix or 'no extension'}"])
    if not Path(path).is_file():
        raise PackageError(path, ["file not found"])
    problems = []
    package, where = readers[Path(path).suffix.lower()](path, problems)
    if not problems:
        problems += [f"{locate(list(e.absolute_path), where)}: {e.message}"
                     for e in jsonschema.Draft202012Validator(SCHEMA).iter_errors(package)]
    if not problems:  # rules the schema cannot express
        lines = [str(r["line"]) for r in package["boq"]]
        problems += [f"BoQ line {n} appears more than once" for n in sorted({n for n in lines if lines.count(n) > 1})]
        names = [b["bidder"] for b in package["bids"]]
        problems += [f"bidder {n!r} appears more than once" for n in sorted({n for n in names if names.count(n) > 1})]
        for j, bid in enumerate(package["bids"]):
            seen = set()
            for k, r in enumerate(bid["lines"]):
                at, line = locate(["bids", j, "lines", k], where), str(r["line"])
                if line not in lines:
                    problems.append(f"{at}: line {line} is not in the BoQ")
                if line in seen:
                    problems.append(f"{at}: line {line} is priced twice")
                seen.add(line)
    if problems:
        raise PackageError(path, problems)
    for r in package["boq"]:
        r["line"] = str(r["line"])
    for bid in package["bids"]:
        for r in bid["lines"]:
            r["line"] = str(r["line"])
    return package, where


def read_package(path):
    """(boq rows, {bidder: rows}) for the checks and the agent; each row carries where it is in the file."""
    package, where = load(path)
    boq = [{"where": locate(["boq", i], where), "line": r["line"], "item": r["item"], "quantity": r["quantity"]}
           for i, r in enumerate(package["boq"])]
    bidders = {bid["bidder"]: [{"where": locate(["bids", j, "lines", k], where), "line": r["line"],
                                "item": r.get("item", ""), "quantity": r.get("quantity"),
                                "unit_price": r["unit_price"], "total": r.get("total")}
                               for k, r in enumerate(bid["lines"])]
               for j, bid in enumerate(package["bids"])}
    return boq, bidders


def describe(path, boq, bidders):
    """What was read, so a misread file is caught before the agent spends minutes on it."""
    out = [f"Read {Path(path).name}: BoQ with {len(boq)} line{'s' if len(boq) != 1 else ''}, {len(bidders)} bidders"]
    for name, rows in bidders.items():
        missing = sorted({r["line"] for r in boq} - {r["line"] for r in rows}, key=lambda x: (len(x), x))
        out.append(f"  {name}: {len(rows)} of {len(boq)} lines priced"
                   + (f" (not priced: {', '.join(missing)})" if missing else ""))
    return "\n".join(out)


def package_lines(boq, bidders):
    """One entry per BoQ line, with every bidder's quoted unit price."""
    lines = {r["line"]: {"line": r["line"], "item": r["item"], "quantity": r["quantity"]} for r in boq}
    for name, rows in bidders.items():
        for r in rows:
            lines[r["line"]].setdefault("prices", {})[name] = r["unit_price"]
    return list(lines.values())


def file_checks(boq, bidders):
    """Problems a committee checks in the bid files themselves. Each one names where in the file it is."""
    found = []

    def flag(check, bidder, r, line=None, **data):
        found.append({"check": check, "bidder": bidder, "line": r["line"] if r else line,
                      "where": r["where"] if r else None, "data": data})

    boq_by_line = {r["line"]: r for r in boq}
    for name, rows in bidders.items():
        for r in rows:
            q, u, t = r["quantity"] or boq_by_line[r["line"]]["quantity"], r["unit_price"], r["total"]
            if q and t is not None and abs(q * u - t) > max(1, 0.001 * abs(t)):
                flag("arithmetic_error", name, r, quantity=q, unit_price=u, total=t)
            expected = boq_by_line[r["line"]]["quantity"]
            if r["quantity"] is not None and r["quantity"] != expected:
                flag("quantity_changed", name, r, quoted=r["quantity"], requested=expected)
        for line in sorted(boq_by_line.keys() - {r["line"] for r in rows}, key=lambda x: (len(x), x)):
            flag("missing_line", name, None, line, item=boq_by_line[line]["item"])
    prices = {name: {r["line"]: r["unit_price"] for r in rows} for name, rows in bidders.items()}
    for a, b in combinations(prices, 2):
        shared = sorted(prices[a].keys() & prices[b].keys(), key=lambda x: (len(x), x))
        same = [line for line in shared if prices[a][line] == prices[b][line]]
        if len(same) >= MIN_IDENTICAL and len(same) * 2 >= len(shared):
            flag("identical_prices", f"{a}, {b}", None, ", ".join(same), same=len(same), shared=len(shared))
    return found


async def analyse(graph, lines, base, batch_id, feedback="", attempt=0, only=None):
    """Run the agent (up to the draft, no gate) once per distinct item; return {line: state}."""
    states, by_item = {}, {}
    for n, entry in enumerate(lines):
        if only is not None and entry["line"] not in only:
            continue
        if not entry.get("prices"):
            continue
        key = entry["item"].lower().strip()
        if key in by_item:  # the same item on another line: one band serves both
            states[entry["line"]] = by_item[key]
            continue
        thread = agent.CURRENT_RUN = f"{batch_id}-{n}" + (f"-r{attempt}" if attempt else "")
        config = {"configurable": {"thread_id": thread}}
        req = {**base, "item": entry["item"], "unit_price": statistics.median(entry["prices"].values())}
        snapshot = await graph.aget_state(config)
        if snapshot.values.get("final") is not None:
            print(f"Line {entry['line']}: reusing the finished result of run {thread}")
        else:
            print(f"\nLine {entry['line']}: {entry['item']} (run {thread})", flush=True)
            agent.audit(thread, event="run_started", request=req, batch=batch_id, line=entry["line"])
            try:
                await graph.ainvoke({"request": req, "feedback": feedback}, config, interrupt_after=["recheck"])
            except Exception as e:  # one failed line must not stop the batch
                agent.audit(thread, event="line_failed", error=repr(e))
                print(f"  ! line {entry['line']} failed: {e!r}", flush=True)
                by_item[key] = states[entry["line"]] = {"request": req, "error": repr(e)[:300]}
                continue
        by_item[key] = states[entry["line"]] = (await graph.aget_state(config)).values
    return states


async def run(args):
    batch_id = args.resume or uuid.uuid4().hex[:12]
    try:
        boq, bidders = read_package(args.sheet)
    except PackageError as e:
        raise SystemExit(str(e))
    print(describe(args.sheet, boq, bidders))
    lines = package_lines(boq, bidders)
    checks = file_checks(boq, bidders)
    names = list(bidders)
    base = {"currency": args.currency, "year": args.year, "country": args.country}
    print(f"Batch {batch_id} | model {agent.MODEL} | {len(lines)} lines, {len(bidders)} bidders")
    agent.audit(batch_id, event="batch_started", sheet=str(args.sheet), bidders=names, lines=len(lines),
                file_checks=checks, model=agent.MODEL)
    async with agent.session() as (graph, tools):
        states = await analyse(graph, lines, base, batch_id)
        attempt = 0
        draft = report.draft_name(Path(args.sheet).stem, batch_id)
        while True:
            agent.save_draft(batch_id, draft, report.build(Path(args.sheet).name, lines, states, names, checks, base,
                                                              batch_id, agent.MODEL), report.summary(lines, states, names, checks, base))
            decision = agent.ask_decision()
            if decision["action"] == "revise":
                redo = input("Which line numbers? (comma-separated, blank for all): ").replace(" ", "")
                decision["lines"] = redo.split(",") if redo else None
            if decision["action"] == "approve":  # the person at this menu is the approval write_price_review needs
                decision["approval"] = grant("write_price_review", draft, decision["name"])
            agent.audit(batch_id, event="human_decision", **decision)
            if decision["action"] != "approve":
                agent.discard_draft(batch_id, draft)
            if decision["action"] != "revise":
                break
            attempt += 1
            states |= await analyse(graph, lines, base, batch_id, decision["feedback"], attempt, decision["lines"])
        saved = None
        if decision["action"] == "approve":
            result = await tools["write_price_review"].ainvoke({"report": draft, "approval": decision["approval"]})
            saved = agent.tool_text(result)
        print(saved or "Not saved: the reviewer rejected the draft.")
        agent.audit(batch_id, event="batch_finished", saved=saved)
