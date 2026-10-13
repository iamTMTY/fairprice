"""fairprice-mcp: price-comparison tools over published OCDS award data.

The model decides which records describe the same product; these tools do the
arithmetic (inflation, currency, price bands) so numbers are never invented.
"""

import datetime as dt
import json
import os
import re
import statistics
import subprocess
import sys
from pathlib import Path

import duckdb
from mcp.server.fastmcp import FastMCP
from sqlalchemy import select

from fairprice.db import (REPORTS, add_step, engine, get_run, request_approval, start_run, steps, supersede,
                          use_approval)
from fairprice.storage import storage

ROOT = Path(__file__).resolve().parents[2]
DB = ROOT / os.environ.get("FAIRPRICE_DB", "data/fairprice.duckdb")  # relative paths are relative to ROOT
FIRST_VALID_YEAR = 2015  # NOCOPO uses 2001-01-01 as a placeholder date
MIN_COMPARABLES = 5
FLAG_RATIO = 1.5  # ponytail: fixed threshold; make it a parameter if a buyer's policy differs

mcp = FastMCP("fairprice", log_level="WARNING")


def db():
    return duckdb.connect(str(DB), read_only=True)


def rows(sql, params=()):
    with db() as con:
        cur = con.execute(sql, params)
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


def data_flags(r):
    flags = []
    if r["year"] is None or not FIRST_VALID_YEAR <= r["year"] <= dt.date.today().year:
        flags.append(f"invalid_date:{r['year']}")
    if (r.get("buyer") or "").upper().startswith("TEST MINISTRY"):  # NOCOPO's own test entries, published as real awards
        flags.append("test_record")
    if r["n_items"] and r["n_items"] > 1:
        flags.append("multi_item_contract")
    if r["unit_price"] is None:
        flags.append("no_quantity_published" if r.get("line_type") == "award" else "no_unit_price")
    return flags


def record_id(value):
    """Accept 113240, "113240" or "L113240"; models write ids all three ways."""
    return int(re.sub(r"\D", "", str(value)))


def index_value(table, country, year):
    """Return (value, year_used); falls back to the latest year published."""
    found = rows(
        f"SELECT value, year FROM {table} WHERE country = ? AND year <= ? ORDER BY year DESC LIMIT 1",
        (country, year),
    )
    return (found[0]["value"], found[0]["year"]) if found else (None, None)


def assess(unit_price, median):
    """(ratio, assessment) of a price against a band median."""
    ratio = unit_price / median
    return round(ratio, 2), (f"above_band: {ratio:.2f}x the median" if ratio >= FLAG_RATIO
                             else f"below_band: {ratio:.2f}x the median" if ratio <= 1 / FLAG_RATIO
                             else "within_band")


def search(keywords, country=None, year_from=None, year_to=None, limit=25):
    """Records whose description or title contains every word of every keyword, in any order (case-insensitive)."""
    words = [w for k in keywords for w in str(k).split()]
    if not words:
        return {"error": "Give at least one keyword."}
    where = ["(description || ' ' || coalesce(title, '')) ILIKE ?" for _ in words]
    params = [f"%{w}%" for w in words]
    for clause, value in (("country = ?", country), ("year >= ?", year_from), ("year <= ?", year_to)):
        if value is not None:
            where.append(clause)
            params.append(value)
    sql = f"FROM lines WHERE {' AND '.join(where)}"
    total = rows(f"SELECT count(*) AS n {sql}", params)[0]["n"]
    found = rows(
        f"SELECT id, country, year, quantity, unit_price, amount, currency, n_items, line_type, description, buyer {sql} "
        f"ORDER BY year DESC NULLS LAST LIMIT ?",
        [*params, max(1, min(limit, 50))],
    )
    for r in found:
        r["flags"] = data_flags(r)
        r["description"] = r["description"][:140] if r["description"] else None
        r["buyer"] = r["buyer"][:60] if r["buyer"] else None
        for k in ("unit_price", "amount"):
            r[k] = round(r[k], 2) if r[k] is not None else None
        del r["n_items"], r["line_type"]  # used for the flags only
    return {"total_matches": total, "returned": len(found), "results": found}


def price_band(unit_price, currency, year, candidate_ids, rejected=None):
    """The band for a price from records judged comparable: excludes flagged records and duplicates, adjusts every
    price to `year` and `currency` (World Bank CPI and FX), and judges the price against the median."""
    rejected = {record_id(k): v for k, v in (rejected or {}).items()}
    if not candidate_ids:
        return {"error": "No candidate_ids given."}
    ids = sorted(set(candidate_ids) | set(rejected))
    recs = rows(
        "SELECT l.*, r.source FROM lines l JOIN releases r USING (country, release_id) "
        f"WHERE l.id IN ({','.join('?' * len(ids))})",
        ids,
    )
    unknown = sorted(set(ids) - {r["id"] for r in recs})
    accepted, excluded, seen, notes = [], [], set(), set()
    for r in sorted(recs, key=lambda r: r["id"]):
        flags = data_flags(r)
        dup_key = (r["buyer"], r["description"], round(r["unit_price"] or 0, 2))
        reason = rejected.get(r["id"]) or (flags and ", ".join(flags)) or (
            "duplicate of an earlier record" if dup_key in seen else None)
        if reason:
            excluded.append({"id": r["id"], "reason": reason})
            continue
        seen.add(dup_key)
        cpi_from, y_from = index_value("cpi", r["country"], r["year"])
        cpi_to, y_to = index_value("cpi", r["country"], year)
        price = r["unit_price"] * cpi_to / cpi_from
        for wanted, used in ((year, y_to), (r["year"], y_from)):
            if wanted != used:
                notes.add(f"CPI for {wanted} not published; used {used}.")
        if r["currency"] != currency:
            fx_from, _ = index_value("fx", r["country"], year)  # local currency per USD
            target = (rows("SELECT country FROM sources WHERE currency = ? LIMIT 1", (currency,)) or [{}])[0].get("country")
            fx_to, _ = index_value("fx", target, year) if target else (None, None)
            if not (fx_from and fx_to):
                excluded.append({"id": r["id"], "reason": f"cannot convert {r['currency']} to {currency}"})
                continue
            price = price / fx_from * fx_to
        accepted.append({"id": r["id"], "ocid": r["ocid"], "year": r["year"], "buyer": r["buyer"],
                         "description": (r["description"] or "")[:140], "quantity": r["quantity"],
                         "published_unit_price": round(r["unit_price"], 2), "currency": r["currency"],
                         "adjusted_unit_price": round(price, 2), "source": r["source"]})
    result = {"price_under_review": unit_price, "currency": currency, "year": year,
              "accepted": accepted, "excluded": excluded, "unknown_ids": unknown, "notes": sorted(notes)}
    prices = sorted(a["adjusted_unit_price"] for a in accepted)
    if len(prices) < MIN_COMPARABLES:
        result["band"] = None
        result["assessment"] = (f"insufficient_comparables: {len(prices)} accepted, need {MIN_COMPARABLES}. "
                                "Search with other keywords, years or countries, or report that no benchmark exists.")
        return result
    q1, median, q3 = statistics.quantiles(prices, n=4)
    result["band"] = {"n": len(prices), "min": prices[0], "p25": round(q1, 2), "median": round(median, 2),
                      "p75": round(q3, 2), "max": prices[-1]}
    result["ratio_to_median"], result["assessment"] = assess(unit_price, median)
    return result


def country_currency(country):
    """The currency a loaded country publishes its awards in, or None if the country is not loaded."""
    found = rows("SELECT currency FROM sources WHERE country = ? AND currency IS NOT NULL "
                 "GROUP BY currency ORDER BY count(*) DESC LIMIT 1", (country.upper(),))
    return found[0]["currency"] if found else None


def review_request(currency, year, country):
    """A review's currency, year and country; the currency defaults to the country's. None if not loaded."""
    currency = (currency or country_currency(country) or "").upper()
    return {"currency": currency, "year": year or dt.date.today().year, "country": country.upper()} if currency else None


NOT_LOADED = "Country {} is not loaded, so it has no currency or past prices. See list_countries; add it with add_country."


# ---------------------------------------------------------------- runs: one review across many calls

def run_or_error(run_id, line):
    run = get_run(run_id)
    if run is None:
        return None, None, {"error": f"No review {run_id!r}. Start one with start_review or start_package_review."}
    entry = next((e for e in run.lines if e["line"] == str(line)), None)
    if entry is None:
        return run, None, {"error": f"Review {run_id} has no line {line!r}; its lines are {[e['line'] for e in run.lines]}."}
    return run, entry, None


def quoted(entry):
    """The price under review for a line: the single price, or the median of the bidders' quotes."""
    prices = list((entry.get("prices") or {}).values())
    return statistics.median(prices) if prices else None


@mcp.tool()
def start_review(item: str, unit_price: float, currency: str = "", year: int | None = None,
                 country: str = "NG") -> dict:
    """Start a price review of one item: what is being bought and the price quoted for it. Returns its run_id;
    every other step of the review uses it, and is recorded under it, so the report is built only from what
    this review's own searches and checks found.

    currency: of the quoted price (default the country's currency).
    year: when the price was quoted (default this year); past prices are adjusted for inflation to it.
    country: ISO code of a loaded country whose past awards to compare with (see list_countries).
    """
    if not item.strip() or unit_price <= 0:
        return {"error": "Give the item and a quoted unit price above zero."}
    if (request := review_request(currency, year, country)) is None:
        return {"error": NOT_LOADED.format(country.upper())}
    lines = [{"line": "1", "item": item.strip(), "quantity": None, "prices": {"Price under review": unit_price}}]
    run_id = start_run("item", item.strip(), request, lines, ["Price under review"])
    return {"run_id": run_id, "lines": lines, **request,
            "next": "plan_searches, then find_comparables, then check_price_comparability, then write_price_review"}


@mcp.tool()
def start_package_review(package: dict, title: str = "bid package", currency: str = "", year: int | None = None,
                         country: str = "NG") -> dict:
    """Start a review of a whole bid package: a bill of quantities and every bidder's price schedule, in the bid
    package format (bid_package.schema.json: {"boq": [{line, item, quantity}], "bids": [{bidder, lines: [{line,
    unit_price, quantity, total}]}]}). A package that does not match is refused with every problem. Returns the
    run_id, its lines and the problems found in the bid files. Review each line with line="<its number>".
    Bidders are never totalled or ranked.
    """
    from fairprice import batch
    try:
        package = batch.validate(json.loads(json.dumps(package)))
    except batch.PackageError as e:
        return {"error": "The package does not match the bid package format.", "problems": e.problems}
    boq, bidders = batch.unpack(package)
    checks = batch.file_checks(boq, bidders)
    lines = batch.package_lines(boq, bidders)
    if (request := review_request(currency, year, country)) is None:
        return {"error": NOT_LOADED.format(country.upper())}
    run_id = start_run("package", title, request, lines, list(bidders), checks)
    return {"run_id": run_id, "lines": lines, "bidders": list(bidders), **request,
            "bid_file_problems": len(checks)}


@mcp.tool()
def plan_searches(run_id: str, searches: list[list[str]], notes: str = "", line: str = "1") -> dict:
    """Record your search plan for a line before searching: the keyword sets you will try, most specific first
    (e.g. [["hilux"], ["hilux", "pickup"], ["pickup"]]), and in notes what you will reject (bundles, other
    models, lot prices). The plan is shown in the report; if a search comes back thin, try the next planned one.
    """
    run, entry, error = run_or_error(run_id, line)
    if error:
        return error
    plan = [[str(w) for w in keys] for keys in searches if keys]
    if not plan:
        return {"error": "Give at least one keyword set, e.g. [[\"hilux\"]]."}
    add_step(run_id, str(line), "plan", {"searches": plan, "notes": notes})
    return {"recorded": True, "line": str(line), "item": entry["item"], "plan": plan}


@mcp.tool()
def find_comparables(run_id: str, keywords: list[str], line: str = "1", year_from: int | None = None,
                     year_to: int | None = None, limit: int = 25) -> dict:
    """Search published contract awards, in the review's country, for records whose description contains every
    word of the keywords, in any order (case-insensitive): ["hilux pickup"] and ["hilux", "pickup"] are the same.

    Use the word that names the product's model (e.g. ["hilux"] or ["probook"]); add words to narrow, remove
    them to broaden. Each result carries data-quality flags. limit: at most 50; total_matches gives the full
    count. The results are recorded under the review: only records found here can be used in its price check.
    """
    run, entry, error = run_or_error(run_id, line)
    if error:
        return error
    found = search(keywords, run.request["country"], year_from, year_to, limit)
    if "error" not in found:
        add_step(run_id, str(line), "search", {"keywords": keywords, "country": run.request["country"],
                                               "matches": found["total_matches"], "ids": [r["id"] for r in found["results"]]})
    return found


@mcp.tool()
def check_price_comparability(run_id: str, candidate_ids: list[int], rejected: dict[str, str] | None = None,
                              line: str = "1") -> dict:
    """Build the price band for a line from the records you judged comparable, and judge the quoted price.

    candidate_ids: records from this review's searches that describe the same product.
    rejected: {id: reason} for records from this review's searches that you judged NOT comparable (another
    model, a bundle, a lot price). They are left out of the band; the reasons appear in the report.
    The price, currency and year come from the review. The tool also excludes records with invalid dates,
    test entries, multi-item contracts, no unit price and duplicates, adjusts every price for inflation and
    currency, and needs at least 5 records for a band. Records not returned by this review's searches are left
    out of the band and listed in left_out.
    """
    run, entry, error = run_or_error(run_id, line)
    if error:
        return error
    if quoted(entry) is None:
        return {"error": f"No bidder priced line {line}, so there is no price to check."}
    rejected = {record_id(k): v for k, v in (rejected or {}).items()}
    candidates = [record_id(i) for i in candidate_ids]
    found = {i for step in steps(run_id, str(line), "search") for i in step.data["ids"]}
    strangers = sorted((set(candidates) | set(rejected)) - found)  # a mistyped id must not sink the other judgements
    candidates = [i for i in candidates if i in found]
    rejected = {k: v for k, v in rejected.items() if k in found}
    if not candidates:
        return {"error": f"Refused: none of the candidate records were returned by this review's searches"
                         f"{f' (not found by them: {strangers})' if strangers else ''}. Search with find_comparables first."}
    result = price_band(quoted(entry), run.request["currency"], run.request["year"], candidates,
                        {str(k): v for k, v in rejected.items()})
    if strangers and "error" not in result:
        result["left_out"] = {"ids": strangers, "reason": "not returned by this review's searches; check the ids"}
    if "error" not in result:
        add_step(run_id, str(line), "check", {"candidate_ids": candidates,
                                              "rejected": {str(k): v for k, v in rejected.items()}, "result": result})
    return result


@mcp.tool()
def challenge_review(run_id: str, verdict: str, reason: str, objections: dict[str, str] | None = None,
                     line: str = "1") -> dict:
    """Record an independent second check of a line's price band: STANDS (a fair benchmark) or DISPUTED, why,
    and objections {id: reason} to records in the band. The report shows it. To take records out, call
    check_price_comparability again with them in `rejected`; objections left in the band are shown as such.
    """
    run, entry, error = run_or_error(run_id, line)
    if error:
        return error
    checks = steps(run_id, str(line), "check")
    if not checks or not checks[-1].data["result"].get("band"):
        return {"error": "There is no price band to challenge on this line yet."}
    in_band = {r["id"] for r in checks[-1].data["result"]["accepted"]}
    objections = {record_id(k): v for k, v in (objections or {}).items()}
    if strangers := sorted(set(objections) - in_band):
        return {"error": f"Refused: records {strangers} are not in this line's band."}
    verdict = verdict.strip().upper()
    if verdict not in ("STANDS", "DISPUTED"):
        return {"error": "verdict must be STANDS or DISPUTED."}
    add_step(run_id, str(line), "challenge", {"verdict": verdict, "reason": reason,
                                              "objections": {str(k): v for k, v in objections.items()}})
    return {"recorded": True, "verdict": verdict, "objections": len(objections)}


def review(run_id):
    """Everything a report needs, rebuilt only from what the run recorded: (run, lines, states)."""
    run = get_run(run_id)
    states, by_item = {}, {}
    for entry in run.lines:
        recorded = steps(run_id, entry["line"])
        checks = [s.data for s in recorded if s.kind == "check"]
        if not checks:
            if entry["item"].lower() in by_item:  # the same item on another line: one band serves both
                states[entry["line"]] = by_item[entry["item"].lower()]
            continue
        final = checks[-1]["result"]
        challenges = [s.data for s in recorded if s.kind == "challenge"]
        challenge = {"verdict": "NOT RUN", "reason": "no independent second check was recorded", "remove": {}}
        if challenges:
            c = challenges[-1]
            remove = {int(k): v for k, v in c["objections"].items()}
            out = {e["id"] for e in final.get("excluded", [])}
            challenge = {"verdict": c["verdict"], "reason": c["reason"], "remove": remove,
                         "applied": all(i in out for i in remove) if remove else None}
        plans = [s.data for s in recorded if s.kind == "plan"]
        state = {"request": {**run.request, "item": entry["item"], "unit_price": quoted(entry)}, "final": final,
                 "challenge": challenge, "plan": plans[-1] if plans else None,
                 "searches": [{"keywords": s.data["keywords"], "country": s.data["country"], "matches": s.data["matches"]}
                              for s in recorded if s.kind == "search"]}
        states[entry["line"]] = by_item.setdefault(entry["item"].lower(), state)
    return run, run.lines, states


def version(run_id):
    """A draft is of a run at one moment: the id of its last recorded step."""
    recorded = steps(run_id)
    return f"{run_id}@{recorded[-1].id if recorded else 0}"


def discard_review(run_id):
    """A rejected or revised draft: delete its files and row, and cancel its pending request."""
    with engine().begin() as conn:
        rows_ = conn.execute(select(REPORTS).where(REPORTS.c.run_id == run_id, REPORTS.c.status == "draft")).all()
        conn.execute(REPORTS.delete().where(REPORTS.c.run_id == run_id, REPORTS.c.status == "draft"))
    store = storage()
    for row in rows_:
        for file in row.files or []:
            store.delete(file)
    supersede("write_price_review", f"{run_id}@")


@mcp.tool()
def write_price_review(run_id: str, approval: str = "") -> dict:
    """Write a review's report. A person must approve it before it becomes final.

    Without approval: builds the draft only from what this review recorded (its plan, searches, checks and
    second check), saves it, and replies with where it is and the command a person runs to approve it
    (`fairprice --approve <request>`). With approval (a person's approval of this exact version of the review):
    writes the final report under that person's name. If the review changed after the draft was made, the
    approval is refused; write the draft again. Refused if the report cites no records or records that do not exist.
    """
    from fairprice import report as rep  # report imports this module

    run = get_run(run_id)
    if run is None:
        return {"error": f"No review {run_id!r}."}
    run, lines, states = review(run_id)
    if not states:
        return {"error": "Refused: nothing to report yet; run check_price_comparability first."}
    cited = sorted({r["id"] for state in states.values() for key in ("accepted", "excluded")
                    for r in (state.get("final") or {}).get(key, [])})
    if not cited:
        return {"error": "Refused: the report cites no source records."}
    known = {r["id"] for r in rows(f"SELECT id FROM lines WHERE id IN ({','.join('?' * len(cited))})", cited)}
    if missing := sorted(set(cited) - known):
        return {"error": f"Refused: cited records do not exist: {missing}"}
    data = rep.build(run.title, lines, states, run.bidders, run.checks if run.kind == "package" else None,
                     run.request, run_id)
    costs = steps(run_id, kind="cost")
    if costs:
        data["report"]["cost"] = costs[-1].data
    target, store, fmt = version(run_id), storage(), os.environ.get("FAIRPRICE_REPORT_FORMAT", "xlsx")
    if not approval:
        discard_review(run_id)  # one draft per review: the latest
        name = rep.draft_name(run.title, run_id)
        written = rep.files(data, name, fmt)
        for file, content in written.items():
            store.save(file, content)
        with engine().begin() as conn:
            conn.execute(REPORTS.insert().values(name=name, run_id=run_id, status="draft", format=fmt,
                                                 files=list(written), created_at=dt.datetime.now(dt.timezone.utc),
                                                 data=data))
        request = request_approval("write_price_review", target)
        return {"status": "needs_approval", "request": request, "draft": name,
                "files": [store.location(f) for f in written],
                "message": f"A person must approve this report. In the terminal: uv run fairprice --approve {request}"}
    granted = use_approval(approval, "write_price_review", target)
    if granted is None:
        return {"error": "Refused: that is not an unused human approval for this version of the review. "
                         "If the review changed, call write_price_review without approval for a new draft."}
    with engine().connect() as conn:
        draft = conn.execute(select(REPORTS).where(REPORTS.c.run_id == run_id, REPORTS.c.status == "draft")).first()
    now = dt.datetime.now().astimezone()
    data["report"].update(status="approved", approved_by=granted.approved_by, approved_at=now.isoformat(timespec="seconds"))
    name = f"{now:%Y%m%d-%H%M%S}-{rep.draft_name(run.title, run_id).removeprefix('DRAFT-')}"
    fmt = draft.format if draft else fmt
    written = rep.files(data, name, fmt)
    for file, content in written.items():
        store.save(file, content)
    discard_review(run_id)
    with engine().begin() as conn:
        conn.execute(REPORTS.insert().values(name=name, run_id=run_id, status="approved", format=fmt,
                                             files=list(written), approved_by=granted.approved_by,
                                             created_at=now, approved_at=now, data=data))
    return {"saved": name, "run_id": run_id, "approved_by": granted.approved_by,
            "files": [store.location(f) for f in written], "sources": len(cited)}


@mcp.tool()
def get_price_review(run_id: str = "") -> dict:
    """An approved review's report as JSON. Without a run_id, list the approved reviews, newest first.

    With a run_id: the report's summary and approval, the price of every line and bidder, the past purchases
    it cites (with their official record ids), each line's plan and searches, and where its files are.
    """
    with engine().connect() as conn:
        if not run_id:
            found = conn.execute(select(REPORTS.c.run_id, REPORTS.c.name, REPORTS.c.approved_by, REPORTS.c.approved_at,
                                        REPORTS.c.format).where(REPORTS.c.status == "approved")
                                 .order_by(REPORTS.c.approved_at.desc()).limit(50)).all()
            return {"reports": [{"run_id": r.run_id, "name": r.name, "approved_by": r.approved_by, "format": r.format,
                                 "approved_at": r.approved_at.isoformat() if r.approved_at else None} for r in found]}
        row = conn.execute(select(REPORTS).where(REPORTS.c.run_id == run_id, REPORTS.c.status == "approved")
                           .order_by(REPORTS.c.id.desc())).first()
    if row is None:
        return {"error": f"No approved report for review {run_id!r}. Call get_price_review without a run_id to list them."}
    store = storage()
    return {**row.data, "files": [store.location(f) for f in row.files or []]}


@mcp.tool()
def list_countries() -> dict:
    """Countries with procurement data on the OCP Data Registry, which are loaded, and the progress of add_country."""
    from fairprice import ingest
    have = {p["country"] for p in ingest.manifest()}
    found = sorted(ingest.countries(ingest.registry()).values(), key=lambda c: c["code"])
    state = ingest.status()
    if state.get("state") == "running" and not running(state.get("pid")):
        state = {**state, "state": "stopped", "message": "the ingest process ended without finishing; see the log"}
    return {"countries": [{**c, "loaded": c["code"] in have} for c in found], "ingest": state}


@mcp.tool()
def add_country(code: str, approval: str = "") -> dict:
    """Load a country's procurement data by ISO code (e.g. "RW"): every publication it has on the OCP Data Registry,
    plus World Bank inflation and exchange rates. A person must approve it first.

    approval: the id of a person's approval to add this country. Without one, this records a request and replies
    with the command a person runs in the terminal to approve it (`fairprice --approve <id>`). Once approved, it runs
    in the background for several minutes (large countries longer); call list_countries to follow progress.
    """
    from fairprice import ingest
    code = code.strip().upper()
    state = ingest.status()
    if state.get("state") == "running" and running(state.get("pid")):
        return {"error": f"An ingest is already running ({state.get('countries')}); see list_countries."}
    if code not in ingest.countries(ingest.registry()):
        return {"error": f"No data on the OCP Data Registry for {code!r}. Call list_countries for the codes."}
    if not approval:
        request = request_approval("add_country", code)
        return {"status": "needs_approval", "request": request,
                "message": f"A person must approve adding {code}. In the terminal: uv run fairprice --approve {request}"}
    if use_approval(approval, "add_country", code) is None:
        return {"error": f"Refused: that is not an unused human approval to add {code}."}
    log = ROOT / "data" / "ingest.log"
    log.parent.mkdir(exist_ok=True)
    with log.open("a") as out:
        proc = subprocess.Popen([sys.executable, "-m", "fairprice.ingest", "--add", code], stdout=out,
                                stderr=subprocess.STDOUT, start_new_session=True)  # keeps running if this server stops
    ingest.status(state="running", countries=[code], pid=proc.pid, finished=None, message=None,
                  started=dt.datetime.now().isoformat(timespec="seconds"))
    return {"started": code, "log": str(log),
            "message": "Downloading and rebuilding in the background; call list_countries to see when it is done."}


def running(pid):
    try:
        os.kill(pid, 0)  # signal 0 only checks that the process exists
        return True
    except (TypeError, OSError):
        return False


@mcp.resource("ocds://line/{line_id}")
def source_record(line_id: str) -> str:
    """The published OCDS record behind a record id (the "FairPrice audit ID" in a report's sources)."""
    found = rows(
        "SELECT l.*, r.source, r.doc FROM lines l JOIN releases r USING (country, release_id) WHERE l.id = ?",
        (int(line_id),),
    )
    if not found:
        return json.dumps({"error": f"no record L{line_id}"})
    r = found[0]
    r["doc"] = json.loads(r["doc"])
    return json.dumps(r, default=str)


def main():
    mcp.run()


if __name__ == "__main__":
    main()
