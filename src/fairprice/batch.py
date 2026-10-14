"""Review a bid package: every bidder's price schedule for one procurement, line by line.

Usage:
  uv run fairprice --package examples/bid-package.xlsx --year 2025     (or .csv, or .json)
  or, from any MCP client, the start_package_review tool with the package as JSON

A package must match bid_package.schema.json exactly; one that does not is refused with every problem and where it
is, never interpreted. Spreadsheets use the column headings of a PPRA (Kenya) price schedule; HEADINGS lists the
ones accepted for each field. The three forms of the same package:
  xlsx  a sheet named "BoQ" (Item No, Description, Unit, Quantity, and optionally Budget per unit), optionally a sheet
        named "Documents" (Ref, Document, Rule, then one column per bidder: the committee's checklist of required
        documents), and one sheet per bidder, named after the bidder (Item No, Unit Price, and optionally
        Description, Unit, Quantity, Total Price, Country of origin)
  csv   one file with a Bidder column and the same headings; rows whose bidder is "BoQ" are the BoQ. A csv holds one
        table, so it has no Documents checklist: use xlsx or json for that
  json  {"boq": [{line, item, unit, quantity, budget}],
         "documents": [{ref, document, rule, bidders: {bidder: value}}],
         "bids": [{bidder, lines: [{line, unit_price, item, unit, quantity, total, country_of_origin}]}]}
A document rule is "Submitted", "Valid on YYYY-MM-DD" or "Covers YYYY-YYYY"; a bidder's value is "Yes" or "No" for
Submitted, the date the document is valid until (or No) for Valid on, and the years it covers (or No) for Covers.
The agent builds one price band per distinct item, then code compares every bidder's price with it and checks the
bid files. Bidders are never totalled, ranked or recommended.
"""

import csv
import datetime as dt
import json
import re
import statistics
from itertools import combinations
from pathlib import Path

import jsonschema
from openpyxl import load_workbook

from fairprice import agent, report, server
from fairprice.db import get_run, start_run

MIN_IDENTICAL = 2  # ponytail: fixed threshold for the identical-prices pattern; tune with committee feedback
SCHEMA = json.loads(Path(__file__).with_name("bid_package.schema.json").read_text())
BOQ_COLUMNS = ("line", "item", "unit", "quantity", "budget")
BID_COLUMNS = ("line", "item", "unit", "quantity", "unit_price", "total", "country_of_origin")
CSV_COLUMNS = ("bidder", *dict.fromkeys(BOQ_COLUMNS + BID_COLUMNS))
NUMERIC = {"quantity", "unit_price", "total", "budget"}
HEADINGS = {  # field: the spreadsheet headings for it (PPRA price schedules), compared after plain()
    "bidder": ("Bidder", "Tenderer"),
    "line": ("Item No", "Item No.", "Item Number"),
    "item": ("Description", "Description of Goods"),
    "unit": ("Unit", "Unit of Issue", "Unit of Measure"),
    "quantity": ("Quantity", "Qty"),
    "unit_price": ("Unit Price", "Rate", "Unit Rate"),
    "total": ("Total Price", "Total Amount", "Total"),
    "country_of_origin": ("Country of Origin",),
    "budget": ("Budget per unit", "Budget unit price"),
}


def plain(heading):
    """A heading without case, punctuation at the end, notes in brackets or a currency: "Rate in KES (VAT
    inclusive)" -> "rate"."""
    h = re.sub(r"\(.*?\)", " ", str(heading).lower())
    h = re.sub(r"\bin [a-z]{3}\b", " ", h)
    return " ".join(h.split()).rstrip(".:")


FIELD = {plain(h): field for field, hs in HEADINGS.items() for h in hs}
DOC_COLUMNS = ("ref", "document", "rule")  # then one column per bidder
RULE = re.compile(r"^(?:(submitted)|valid on (\d{4}-\d{2}-\d{2})|covers (\d{4})(?:\s*[-–]\s*(\d{4}))?)$", re.I)
YEARS = re.compile(r"^(\d{4})(?:\s*[-–]\s*(\d{4}))?$")


def rule_of(text):
    """("submitted", None) | ("valid", date) | ("covers", {years}), or None if the rule is not one of the three."""
    m = RULE.match(" ".join(str(text).split()))
    if not m:
        return None
    if m[1]:
        return "submitted", None
    if m[2]:
        try:
            return "valid", dt.date.fromisoformat(m[2])
        except ValueError:
            return None
    return "covers", set(range(int(m[3]), int(m[4] or m[3]) + 1))


def value_of(kind, text):
    """What a checklist cell says under a rule: False (not submitted), True, a date or {years}; None if unreadable."""
    t = str(text).strip()
    if t.lower() in ("yes", "no"):
        return (t.lower() == "yes") if kind == "submitted" or t.lower() == "no" else None
    if kind == "valid":
        try:
            return dt.date.fromisoformat(t)
        except ValueError:
            return None
    if kind == "covers" and (m := YEARS.match(t)):
        return set(range(int(m[1]), int(m[2] or m[1]) + 1))
    return None


SHOULD = {"submitted": "Yes or No", "valid": "the date it is valid until (YYYY-MM-DD), or No",
          "covers": "the years it covers (such as 2022-2024), or No"}
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
    written = [str(c).strip() if c not in (None, "") else "" for c in rows[0]]
    header = [FIELD.get(plain(h), "?") if h else "" for h in written]
    if unknown := [w for w, h in zip(written, header) if h not in allowed and h]:
        problems.append(f"{label}, row 1: unknown column(s) {', '.join(unknown)}; the columns are "
                        f"{', '.join(HEADINGS[c][0] for c in allowed)}")
    if twice := sorted({h for h in header if h and h != "?" and header.count(h) > 1}):
        problems.append(f"{label}, row 1: more than one column for {', '.join(HEADINGS[c][0] for c in twice)}")
    if missing := [c for c in required if c not in header]:
        problems.append(f"{label}, row 1: missing column(s) {', '.join(HEADINGS[c][0] for c in missing)}")
    if unknown or missing or twice:
        return []
    out = []
    for n, r in enumerate(rows[1:], start=2):
        where = f"{label}, row {n}"
        if any(v not in (None, "") for h, v in zip(header, r) if not h) or len(r) > len(header):
            problems.append(f"{where}: a value in a column with no name")
        values = {h: cell(v, f"{where}, {w}", problems, h in NUMERIC) for w, h, v in zip(written, header, r) if h}
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
        if name.strip().lower() == "documents":
            package["documents"] = read_documents(rows, label, where, problems)
        elif name.strip().lower() == "boq":
            for w, r in records(rows, BOQ_COLUMNS, ("line", "item", "quantity"), label, problems):
                where[("boq", len(package["boq"]))] = w
                package["boq"].append(r)
        else:
            bid = {"bidder": name.strip(), "lines": []}
            for w, r in records(rows, BID_COLUMNS, ("line", "unit_price"), label, problems):
                where[("bids", len(package["bids"]), "lines", len(bid["lines"]))] = w
                bid["lines"].append(r)
            package["bids"].append(bid)
    return package, where


def read_documents(rows, label, where, problems):
    """The Documents checklist: Ref, Document, Rule, then one column per bidder, named exactly as its sheet."""
    rows = [list(r) for r in rows if any(c not in (None, "") for c in r)]
    if not rows:
        problems.append(f"{label}: no rows")
        return []
    head = [str(c).strip() if c not in (None, "") else "" for c in rows[0]]
    if [plain(h) for h in head[:3]] != list(DOC_COLUMNS):
        problems.append(f"{label}, row 1: the first columns must be Ref, Document, Rule, then one column per bidder")
        return []
    bidders = head[3:]
    if "" in bidders:
        problems.append(f"{label}, row 1: a bidder column with no name")
        return []
    out = []
    for n, r in enumerate(rows[1:], start=2):
        r = r + [None] * (len(head) - len(r))
        where[("documents", len(out))] = f"{label}, row {n}"
        for b in bidders:
            where[("documents", len(out), "bidders", b)] = f"{label}, row {n}, {b}"
        text = lambda v: v.date().isoformat() if isinstance(v, dt.datetime) else (
            str(int(v)) if isinstance(v, float) and v.is_integer() else "" if v is None else str(v).strip())
        out.append({"ref": text(r[0]), "document": text(r[1]), "rule": text(r[2]),
                    "bidders": {b: text(v) for b, v in zip(bidders, r[3:])}})
    return out


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
                problems.append(f"{w}: a BoQ row has no {', '.join(HEADINGS[c][0] for c in extra)}")
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


def validate(package, where=None, label="package"):
    """Check a package (the schema's form) against the schema and the rules it cannot express; raise PackageError
    with every problem and where it is. Returns the package with line numbers as text."""
    where = where or {}
    problems = [f"{locate(list(e.absolute_path), where)}: {e.message}"
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
        refs = [str(d["ref"]) for d in package.get("documents", [])]
        problems += [f"document ref {n} appears more than once" for n in sorted({n for n in refs if refs.count(n) > 1})]
        for i, d in enumerate(package.get("documents", [])):
            at, rule = locate(["documents", i], where), rule_of(d["rule"])
            if rule is None:
                problems.append(f"{at}: rule {d['rule']!r} is not Submitted, Valid on YYYY-MM-DD or Covers YYYY-YYYY")
                continue
            if unknown := sorted(set(d["bidders"]) - set(names)):
                problems.append(f"{at}: {', '.join(unknown)} is not a bidder in this package")
            if absent := [n for n in names if n not in d["bidders"]]:
                problems.append(f"{at}: no column for {', '.join(absent)}")
            for b, v in d["bidders"].items():
                if b in names and value_of(rule[0], v) is None:
                    problems.append(f"{locate(['documents', i, 'bidders', b], where)}: {str(v)!r}; write {SHOULD[rule[0]]}")
    if problems:
        raise PackageError(label, problems)
    for r in package["boq"]:
        r["line"] = str(r["line"])
    for bid in package["bids"]:
        for r in bid["lines"]:
            r["line"] = str(r["line"])
    return package


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
    if problems:
        raise PackageError(path, problems)
    return validate(package, where, path), where


def unpack(package, where=None):
    """(boq rows, {bidder: rows}) for the checks and the agent; each row carries where it is in the file."""
    where = where or {}
    boq = [{"where": locate(["boq", i], where), "line": r["line"], "item": r["item"], "quantity": r["quantity"],
            "unit": r.get("unit"), "budget": r.get("budget")} for i, r in enumerate(package["boq"])]
    bidders = {bid["bidder"]: [{"where": locate(["bids", j, "lines", k], where), "line": r["line"],
                                "item": r.get("item", ""), "quantity": r.get("quantity"),
                                "unit_price": r["unit_price"], "total": r.get("total")}
                               for k, r in enumerate(bid["lines"])]
               for j, bid in enumerate(package["bids"])}
    return boq, bidders


def template(path):
    """Write a blank bid package to fill in; returns what to do next. Never overwrites a file."""
    path, kind = Path(path), Path(path).suffix.lower()
    if kind not in (".xlsx", ".csv", ".json"):
        raise SystemExit(f"use .xlsx, .csv or .json, not {kind or 'no extension'}")
    if path.exists():
        raise SystemExit(f"{path} already exists; choose another name")
    boq = [HEADINGS[c][0] for c in BOQ_COLUMNS]
    bid = [HEADINGS[c][0] for c in BID_COLUMNS if c != "country_of_origin"]
    if kind == ".json":
        path.write_text(json.dumps({"boq": [{"line": "1", "item": "", "unit": "", "quantity": 1, "budget": 0}],
                                    "documents": [{"ref": "1", "document": "", "rule": "Submitted",
                                                   "bidders": {"Bidder 1": "Yes"}}],
                                    "bids": [{"bidder": "Bidder 1", "lines": [{"line": "1", "unit_price": 0}]}]},
                                   indent=2) + "\n")
    elif kind == ".csv":
        path.write_text(",".join([HEADINGS["bidder"][0], *dict.fromkeys(boq + bid)]) + "\n")
    else:
        from openpyxl import Workbook
        from openpyxl.comments import Comment
        wb = Workbook()
        wb.active.title = "BoQ"
        wb["BoQ"].append(boq)
        docs = wb.create_sheet("Documents")
        docs.append(["Ref", "Document", "Rule", "Bidder 1", "Bidder 2"])
        docs["C1"].comment = Comment("One of: Submitted / Valid on YYYY-MM-DD / Covers YYYY-YYYY", "FairPrice")
        docs["D1"].comment = Comment("One column per bidder, named exactly as the bidder's sheet. Write Yes or No; "
                                     "for Valid on, the date the document is valid until; for Covers, the years "
                                     "it covers (such as 2022-2024).", "FairPrice")
        for name in ("Bidder 1", "Bidder 2"):
            wb.create_sheet(name).append(bid)
        wb.save(path)
    return (f"Wrote {path}. Fill in the BoQ"
            + (", the documents the tender requires (Documents)" if kind != ".csv" else "")
            + ", and each bidder's price schedule"
            + (" (one sheet per bidder, named after the bidder; rename or add sheets)" if kind == ".xlsx" else "")
            + f". Then: uv run fairprice --package {path}")


def read_package(path):
    return unpack(*load(path))


def describe(path, boq, bidders, documents=None):
    """What was read, so a misread file is caught before the agent spends minutes on it."""
    out = [f"Read {Path(path).name}: BoQ with {len(boq)} line{'s' if len(boq) != 1 else ''}, {len(bidders)} bidders, "
           + (f"{len(documents)} required documents" if documents else "no documents checklist")]
    for name, rows in bidders.items():
        missing = sorted({r["line"] for r in boq} - {r["line"] for r in rows}, key=lambda x: (len(x), x))
        out.append(f"  {name}: {len(rows)} of {len(boq)} lines priced"
                   + (f" (not priced: {', '.join(missing)})" if missing else ""))
    return "\n".join(out)


def requirements(package):
    """The tender's required documents, as the report lists them."""
    return [{"ref": str(d["ref"]), "document": d["document"], "rule": " ".join(d["rule"].split())}
            for d in package.get("documents", [])]


def document_checks(package, where=None):
    """Documents a bidder did not submit, that expire before the date the tender requires, or that miss years.
    Checked as the committee recorded them: FairPrice cannot tell whether a certificate is genuine."""
    found = []
    for i, d in enumerate(package.get("documents", [])):
        kind, need = rule_of(d["rule"])
        for b, v in d["bidders"].items():
            got = value_of(kind, v)
            data = {"ref": str(d["ref"]), "document": d["document"]}
            at = locate(["documents", i, "bidders", b], where or {})
            if got is False:
                found.append({"check": "document_missing", "bidder": b, "line": None, "where": at, "data": data})
            elif kind == "valid" and got < need:
                found.append({"check": "document_expired", "bidder": b, "line": None, "where": at,
                              "data": {**data, "valid_until": got.isoformat(), "needed": need.isoformat()}})
            elif kind == "covers" and (missing := sorted(need - got)):
                found.append({"check": "document_years", "bidder": b, "line": None, "where": at,
                              "data": {**data, "covers": str(v), "missing": missing,
                                       "needed": f"{min(need)}–{max(need)}" if len(need) > 1 else str(min(need))}})
    return found


def package_lines(boq, bidders):
    """One entry per BoQ line, with every bidder's quoted unit price."""
    lines = {r["line"]: {"line": r["line"], "item": r["item"], "quantity": r["quantity"],
                         **({"budget": r["budget"]} if r.get("budget") is not None else {})} for r in boq}
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


async def analyse(graph, lines, base, run_id, feedback="", attempt=0, only=None, usage=None):
    """Run the agent (up to the draft, no gate) once per distinct item of the run; return {line: state}.
    Every search and check is recorded under the run, line by line."""
    states, by_item = {}, {}
    agent.CURRENT_RUN = run_id
    for n, entry in enumerate(lines):
        if only is not None and entry["line"] not in only:
            continue
        if not entry.get("prices"):
            continue
        key = entry["item"].lower().strip()
        if key in by_item:  # the same item on another line: one band serves both
            states[entry["line"]] = by_item[key]
            continue
        thread = f"{run_id}-{n}" + (f"-r{attempt}" if attempt else "")
        config = {"configurable": {"thread_id": thread}, "callbacks": [usage] if usage else []}
        req = {**base, "run_id": run_id, "line": entry["line"], "item": entry["item"],
               "unit_price": statistics.median(entry["prices"].values())}
        snapshot = await graph.aget_state(config)
        if snapshot.values.get("final") is not None:
            print(f"Line {entry['line']}: reusing the finished result")
        else:
            print(f"\nLine {entry['line']}: {entry['item']}", flush=True)
            try:
                await graph.ainvoke({"request": req, "feedback": feedback}, config, interrupt_after=["recheck"])
            except Exception as e:  # one failed line must not stop the batch
                agent.audit(run_id, event="line_failed", line=entry["line"], error=repr(e))
                print(f"  ! line {entry['line']} failed: {e!r}", flush=True)
                by_item[key] = states[entry["line"]] = {"request": req, "error": repr(e)[:300]}
                continue
        by_item[key] = states[entry["line"]] = (await graph.aget_state(config)).values
    return states


async def run(args):
    try:
        package, where = load(args.sheet)
    except PackageError as e:
        raise SystemExit(str(e))
    boq, bidders = unpack(package, where)
    print(describe(args.sheet, boq, bidders, package.get("documents")))
    lines = package_lines(boq, bidders)
    checks = document_checks(package, where) + file_checks(boq, bidders)  # each with its sheet and row
    names = list(bidders)
    base = server.review_request(args.currency, args.year, args.country)
    if base is None:
        raise SystemExit(server.NOT_LOADED.format(args.country.upper()))
    request = {**base, "documents": requirements(package)} if package.get("documents") else base
    run_id = args.resume if args.resume and get_run(args.resume) else start_run(
        "package", Path(args.sheet).name, request, lines, names, checks)
    print(f"Review {run_id} | model {agent.MODEL} | {len(lines)} lines, {len(bidders)} bidders")
    agent.audit(run_id, event="run_started", sheet=str(args.sheet), bidders=names, lines=len(lines),
                file_checks=checks, model=agent.MODEL)

    def which_lines():
        redo = input("Which line numbers? (comma-separated, blank for all): ").replace(" ", "")
        return redo.split(",") if redo else None
    async with agent.session() as (graph, tools):
        usage = agent.Usage()
        states = await analyse(graph, lines, base, run_id, usage=usage)
        attempt = 0
        while True:
            decision = await agent.gate(run_id, tools, report.summary(lines, states, names, checks, request), usage,
                                        ask_lines=which_lines)
            if decision["action"] != "revise":
                break
            attempt += 1
            states |= await analyse(graph, lines, base, run_id, decision["feedback"], attempt, decision["lines"], usage)
        saved = None
        if decision["action"] == "approve":
            result = await tools["write_price_review"].ainvoke({"run_id": run_id, "approval": decision["approval"]})
            saved = agent.tool_text(result)
        print(saved or "Not saved: the reviewer rejected the draft.")
        agent.audit(run_id, event="run_finished", saved=saved, cost=usage.summary())
