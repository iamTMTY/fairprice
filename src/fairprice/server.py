"""fairprice-mcp: the product. Tools for reviewing a tender's prices against published OCDS award data, for any
MCP client: FairPrice's own CLI, another AI app, or a procurement system.

The model decides which records describe the same product; these tools do the arithmetic (inflation, currency,
price bands) so numbers are never invented. Every call is audited here, where it runs, whoever the caller is.

  fairprice-server                          stdio, for an app on this computer (no keys: organisation "local")
  fairprice-server --http [--host --port]   a web server: MCP at /mcp; every caller needs an access key, and
                                            sees only its own organisation's reviews
  fairprice-server --add-key ORG --name NAME     (--revoke-key ID)

FairPrice never ranks bidders, recommends one or decides anything: it prepares evidence, and a named person
approves each report (write_price_review's approved_by). The committee decides.
"""

import argparse
import base64
import binascii
import datetime as dt
import functools
import hashlib
import hmac
import inspect
import json
import os
import re
import secrets
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import duckdb
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken
from mcp.server.auth.settings import AuthSettings
from mcp.server.fastmcp import FastMCP
from sqlalchemy import select
from starlette.requests import Request
from starlette.responses import Response

from fairprice.db import CALLER, REPORTS, add_key, add_step, audit, check_key, engine, get_run, org, revoke_key, start_run, steps
from fairprice.storage import storage

ROOT = Path(__file__).resolve().parents[2]
DB = ROOT / os.environ.get("FAIRPRICE_DB", "data/fairprice.duckdb")  # relative paths are relative to ROOT
FIRST_VALID_YEAR = 2015  # NOCOPO uses 2001-01-01 as a placeholder date
MIN_COMPARABLES = 5
FLAG_RATIO = 1.5  # ponytail: fixed threshold; make it a parameter if a buyer's policy differs
EXTREME = 10  # ponytail: a price this many times from the band's median is a data error, not a market price

PUBLIC_URL = os.environ.get("FAIRPRICE_PUBLIC_URL", "http://127.0.0.1:8000").rstrip("/")  # where people reach it
SECRET = os.environ.get("FAIRPRICE_SECRET") or secrets.token_hex(16)  # signs report download links


class KeyVerifier:
    """Over HTTP, every call needs an organisation's access key."""

    async def verify_token(self, token):
        who = check_key(token)
        return AccessToken(token=token, client_id=who["key"], scopes=[]) if who else None


mcp = FastMCP("fairprice", log_level="WARNING", stateless_http=True, json_response=True,
              token_verifier=KeyVerifier(),
              auth=AuthSettings(issuer_url=PUBLIC_URL, resource_server_url=f"{PUBLIC_URL}/mcp",
                                validate_token_resource=False))  # our keys are checked by KeyVerifier, not an issuer


def caller():
    """The caller of this request: an app's key over HTTP, or None on this computer (organisation "local")."""
    token = get_access_token()
    return check_key(token.token) if token else None


def tool(fn):
    """An MCP tool that knows its caller and writes its own audit row: inputs, output and time."""
    sig = inspect.signature(fn)

    @functools.wraps(fn)
    def call(*args, **kwargs):
        who = CALLER.get() or caller()
        token = CALLER.set(who)
        given = sig.bind(*args, **kwargs)
        given.apply_defaults()
        shown = {k: f"<file, {len(v)} characters>" if k == "file" and v else v for k, v in given.arguments.items()}
        start = time.perf_counter()
        try:
            out = fn(*args, **kwargs)
        except Exception as e:
            audit(shown.get("run_id"), server="fairprice", tool=fn.__name__, args=shown, error=repr(e),
                  caller=(who or {}).get("name") or os.environ.get("FAIRPRICE_CALLER"))
            raise
        else:
            run_id = shown.get("run_id") or (out.get("run_id") if isinstance(out, dict) else None)
            audit(run_id, server="fairprice", tool=fn.__name__, args=shown,
                  output=json.dumps(out, default=str, ensure_ascii=False), seconds=round(time.perf_counter() - start, 2),
                  caller=(who or {}).get("name") or os.environ.get("FAIRPRICE_CALLER"))
            return out
        finally:
            CALLER.reset(token)
    return mcp.tool()(call)


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
    if r.get("amount") is not None and r["amount"] <= 0:  # published values of zero or below: an entry error
        flags.append("value_zero_or_negative")
    elif r["unit_price"] is None:
        flags.append("no_quantity_published" if r.get("line_type") == "award" else "no_unit_price")
    elif r["unit_price"] < 1:
        flags.append("price_below_one")
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
    if not candidate_ids and not rejected:
        return {"error": "No candidate_ids or rejected records given."}
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
    if len(accepted) >= 3:  # extreme values publishers left in: typing errors, totals entered as prices per item
        middle = statistics.median(a["adjusted_unit_price"] for a in accepted)
        for a in list(accepted):
            ratio = a["adjusted_unit_price"] / middle
            if ratio > EXTREME or ratio < 1 / EXTREME:
                accepted.remove(a)
                excluded.append({"id": a["id"], "reason": f"extreme_value:{ratio:.3g}"})
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


def loaded_countries():
    """[(code, name)] of the countries whose data is loaded, the first loaded first; [] before any is."""
    try:
        found = rows("SELECT country, min(split_part(title, ':', 1)) AS name FROM sources WHERE country IS NOT NULL "
                     "GROUP BY country ORDER BY min(position)")
    except duckdb.Error:  # no database yet
        return []
    return [(r["country"], r["name"].strip()) for r in found]


def review_request(currency, year, country=""):
    """A review's currency, year and country: one country, by default FAIRPRICE_COUNTRY or else the first loaded;
    the currency defaults to the country's. None if that country is not loaded."""
    loaded = dict(loaded_countries())
    code = (country or os.environ.get("FAIRPRICE_COUNTRY") or next(iter(loaded), "")).upper()
    if code not in loaded:
        return None
    return {"currency": (currency or country_currency(code) or "").upper(), "year": year or dt.date.today().year,
            "country": code}


NOT_LOADED = "Country {} is not loaded, so it has no currency or past prices. See list_countries; add it with add_country."
NO_COUNTRY = "No country's data is loaded yet. Load one with add_country; list_countries shows the codes."


def not_loaded(country):
    return NOT_LOADED.format(country.upper()) if country else NO_COUNTRY


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


@tool
def start_review(item: str, unit_price: float, currency: str = "", year: int | None = None,
                 country: str = "") -> dict:
    """Start a price review of one item: what is being bought and the price quoted for it. Returns its run_id;
    every other step of the review uses it, and is recorded under it, so the report is built only from what
    this review's own searches and checks found.

    currency: of the quoted price (default the country's currency).
    year: when the price was quoted (default this year); past prices are adjusted for inflation to it.
    country: ISO code of a loaded country whose past awards to compare with (default: the server's default country,
    else the first loaded; see list_countries). A review covers one country.
    """
    if not item.strip() or unit_price <= 0:
        return {"error": "Give the item and a quoted unit price above zero."}
    if (request := review_request(currency, year, country)) is None:
        return {"error": not_loaded(country)}
    lines = [{"line": "1", "item": item.strip(), "quantity": None, "prices": {"Price under review": unit_price}}]
    run_id = start_run("item", item.strip(), request, lines, ["Price under review"])
    return {"run_id": run_id, "lines": lines, **request,
            "next": "plan_searches, then find_comparables, then check_price_comparability, then write_price_review"}


@tool
def start_package_review(package: dict | None = None, title: str = "", currency: str = "", year: int | None = None,
                         country: str = "", file: str = "", filename: str = "") -> dict:
    """Start a review of a whole bid package: its bill of quantities and every bidder's price schedule, in the bid
    package format (bid_package.schema.json: {"boq": [{line, item, unit, quantity, budget}], "bids": [{bidder,
    lines: [{line, unit_price, quantity, total}]}]}). A package that does not match is refused with every problem.
    Returns the run_id, its lines, and the problems found in the bid files. Review each line with
    line="<its number>". Bidders are never totalled or ranked.

    Instead of package, send the tender workbook itself: file (its contents, base64) and filename (with .xlsx, .csv
    or .json); problems are then reported with their sheet and row. The format, an example and a blank workbook are
    the resources fairprice://bid-package/schema, /example and /template.xlsx.
    """
    from fairprice import batch
    try:
        if file:
            package, where = read_upload(file, filename)
            title = title or Path(filename).name
        elif package is not None:
            package, where = batch.validate(json.loads(json.dumps(package))), {}
        else:
            return {"error": "Send package (the JSON) or file and filename (the workbook)."}
    except batch.PackageError as e:
        return {"error": "The package does not match the bid package format.", "problems": e.problems}
    boq, bidders = batch.unpack(package, where)
    checks = batch.file_checks(boq, bidders)
    lines = batch.package_lines(boq, bidders)
    if (request := review_request(currency, year, country)) is None:
        return {"error": not_loaded(country)}
    run_id = start_run("package", title or "bid package", request, lines, list(bidders), checks)
    return {"run_id": run_id, "lines": lines, "bidders": list(bidders), **request,
            "bid_file_problems": len(checks)}


def read_upload(file, filename):
    """A workbook sent through MCP, read exactly as the CLI reads a file: (package, where), or PackageError."""
    from fairprice import batch
    try:
        content = base64.b64decode(file, validate=True)
    except (binascii.Error, ValueError):
        raise batch.PackageError(filename or "file", ["file is not base64"])
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / Path(filename or "package").name
        path.write_bytes(content)
        return batch.load(path)


@tool
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


@tool
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


@tool
def check_price_comparability(run_id: str, candidate_ids: list[int], rejected: dict[str, str] | None = None,
                              line: str = "1") -> dict:
    """Build the price band for a line from the records you judged comparable, and judge the quoted price.

    candidate_ids: records from this review's searches that describe the same product.
    rejected: {id: reason} for records from this review's searches that you judged NOT comparable (another
    model, a bundle, a lot price). They are left out of the band; the reasons appear in the report.
    The price, currency and year come from the review. The tool also excludes records with invalid dates,
    test entries, multi-item contracts, no unit price and duplicates, adjusts every price for inflation and
    currency, and needs at least 5 records for a band. Records not returned by this review's searches are left
    out of the band and listed in left_out. If none of the records found is comparable, send candidate_ids=[] with
    each of them in rejected: the review then records that there is no benchmark.
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
    if not candidates and not rejected:  # judging that nothing found is comparable is a result; judging nothing is not
        return {"error": f"Refused: none of the records were returned by this review's searches"
                         f"{f' (not found by them: {strangers})' if strangers else ''}. Search with find_comparables first."}
    result = price_band(quoted(entry), run.request["currency"], run.request["year"], candidates,
                        {str(k): v for k, v in rejected.items()})
    if strangers and "error" not in result:
        result["left_out"] = {"ids": strangers, "reason": "not returned by this review's searches; check the ids"}
    if "error" not in result:
        add_step(run_id, str(line), "check", {"candidate_ids": candidates,
                                              "rejected": {str(k): v for k, v in rejected.items()}, "result": result})
    return result


@tool
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


@tool
def write_price_review(run_id: str, approved_by: str, format: str = "") -> dict:
    """Write a review's report, approved by the named person who read the review: "Approved by <name> on <date>".

    The report is built only from what this review recorded (its plan, searches, checks and second check); the
    caller sends no prices or sources. Call it once a person has read the review (review_status gives a summary)
    and approved it, with their full name. FairPrice never ranks bidders or decides anything; the committee does.
    Refused without a name, before any price check, or if the report would cite no records or records that do not
    exist. format: xlsx, csv or json (default: the server's setting). Writing again saves a new version.
    """
    from fairprice import report as rep  # report imports this module

    if not approved_by.strip():
        return {"error": "Refused: give the full name of the person who approved this report (approved_by)."}
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
    fmt = format or os.environ.get("FAIRPRICE_REPORT_FORMAT", "xlsx")
    if fmt not in rep.FORMATS:
        return {"error": f"format is one of {', '.join(rep.FORMATS)}"}
    data = rep.build(run.title, lines, states, run.bidders, run.checks if run.kind == "package" else None,
                     run.request, run_id)
    costs = steps(run_id, kind="cost")
    if costs:
        data["report"]["cost"] = costs[-1].data
    now = dt.datetime.now().astimezone()
    data["report"].update(status="approved", approved_by=approved_by.strip(), approved_at=now.isoformat(timespec="seconds"))
    name = f"{now:%Y%m%d-%H%M%S}-{rep.file_name(run.title, run_id)}"
    written = rep.files(data, name, fmt)
    store = storage()
    for file, content in written.items():
        store.save(file, content)
    with engine().begin() as conn:
        conn.execute(REPORTS.insert().values(name=name, run_id=run_id, status="approved", format=fmt,
                                             files=list(written), approved_by=approved_by.strip(),
                                             created_at=now, approved_at=now, data=data, org=org()))
    used = sum(s["used"] == "Yes" for s in data["sources"])
    return {"saved": name, "run_id": run_id, "approved_by": approved_by.strip(), "files": [link(f) for f in written],
            "past_purchases_compared": used, "left_out_with_reasons": len(data["sources"]) - used}


@tool
def get_price_review(run_id: str = "") -> dict:
    """An approved review's report as JSON. Without a run_id, list the approved reviews, newest first.

    With a run_id: the report's summary and approval, the price of every line and bidder, the past purchases
    it cites (with their official record ids), each line's plan and searches, and where its files are.
    """
    with engine().connect() as conn:
        if not run_id:
            found = conn.execute(select(REPORTS.c.run_id, REPORTS.c.name, REPORTS.c.approved_by, REPORTS.c.approved_at,
                                        REPORTS.c.format).where(REPORTS.c.status == "approved", REPORTS.c.org == org())
                                 .order_by(REPORTS.c.approved_at.desc()).limit(50)).all()
            return {"reports": [{"run_id": r.run_id, "name": r.name, "approved_by": r.approved_by, "format": r.format,
                                 "approved_at": r.approved_at.isoformat() if r.approved_at else None} for r in found]}
        row = conn.execute(select(REPORTS).where(REPORTS.c.run_id == run_id, REPORTS.c.status == "approved",
                                                 REPORTS.c.org == org()).order_by(REPORTS.c.id.desc())).first()
    if row is None:
        return {"error": f"No approved report for review {run_id!r}. Call get_price_review without a run_id to list them."}
    return {**row.data, "files": [link(f) for f in row.files or []]}


def sign(name, expires):
    return hmac.new(SECRET.encode(), f"{name}|{expires}".encode(), hashlib.sha256).hexdigest()


def link(name):
    """Where a report file can be opened: a signed link for one hour when served over the web (or S3's own),
    otherwise its path on this computer."""
    if os.environ.get("FAIRPRICE_PUBLIC_URL") and os.environ.get("FAIRPRICE_STORAGE", "file") == "file":
        expires = int(time.time()) + 3600
        return f"{PUBLIC_URL}/reports/{name}?expires={expires}&sig={sign(name, expires)}"
    return storage().location(name)


@tool
def list_countries() -> dict:
    """Countries with procurement data on the OCP Data Registry, which are loaded, and the progress of add_country."""
    from fairprice import ingest
    have = {p["country"] for p in ingest.manifest()}
    found = sorted(ingest.countries(ingest.registry()).values(), key=lambda c: c["code"])
    state = ingest.status()
    if state.get("state") == "running" and not running(state.get("pid")):
        state = {**state, "state": "stopped", "message": "the ingest process ended without finishing; see the log"}
    return {"countries": [{**c, "loaded": c["code"] in have} for c in found], "ingest": state}


@tool
def add_country(code: str) -> dict:
    """Load a country's procurement data by ISO code (e.g. "RW"): every publication it has on the OCP Data Registry,
    plus World Bank inflation and exchange rates. Runs in the background for several minutes (large countries
    longer), one country at a time; call list_countries to follow progress.
    """
    from fairprice import ingest
    code = code.strip().upper()
    state = ingest.status()
    if state.get("state") == "running" and running(state.get("pid")):
        return {"error": f"An ingest is already running ({state.get('countries')}); see list_countries."}
    if code not in ingest.countries(ingest.registry()):
        return {"error": f"No data on the OCP Data Registry for {code!r}. Call list_countries for the codes."}
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


@tool
def review_with_fairprice(run_id: str) -> dict:
    """Have FairPrice's own agent review every line of a run in the background: its open model plans and runs the
    searches and price checks, and a second, independent agent tries to disprove each band. For apps that do not
    do the reasoning themselves. A package takes minutes per distinct item; call review_status to follow it, then
    show its summary to a person, and call write_price_review with their name if they approve.
    """
    run = get_run(run_id)
    if run is None:
        return {"error": f"No review {run_id!r}."}
    status = steps(run_id, "*", "status")
    if status and status[-1].data.get("state") == "running" and running(status[-1].data.get("pid")):
        return {"error": "This review is already running; call review_status."}
    log = ROOT / "data" / "reviews" / f"{run_id}.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("a") as out:
        proc = subprocess.Popen([sys.executable, "-m", "fairprice.agent", "--review", run_id], stdout=out,
                                stderr=subprocess.STDOUT, start_new_session=True,
                                env={**os.environ, "FAIRPRICE_ORG": org(),  # the agent works as this organisation
                                     "FAIRPRICE_CALLER": f"FairPrice agent (for {(CALLER.get() or {}).get('name', 'this computer')})"})
    add_step(run_id, "*", "status", {"state": "running", "pid": proc.pid})
    return {"started": run_id, "lines": len(run.lines), "message": "Reviewing in the background; call review_status."}


@tool
def review_status(run_id: str) -> dict:
    """How far FairPrice's own review of a run has got (review_with_fairprice): running, done or failed, how many
    lines have a price check, and when done a summary for a person to read before approving."""
    run = get_run(run_id)
    if run is None:
        return {"error": f"No review {run_id!r}."}
    status = steps(run_id, "*", "status")
    last = dict(status[-1].data) if status else {"state": "not started"}
    if last["state"] == "running" and not running(last.get("pid")):
        last = {"state": "failed", "error": "the review process ended without finishing"}
    checked = {s.line for s in steps(run_id, kind="check")}
    last.pop("pid", None)
    return {"run_id": run_id, **last, "lines_checked": f"{len(checked)} of {len(run.lines)}"}


@mcp.resource("fairprice://bid-package/schema", mime_type="application/schema+json")
def package_schema() -> str:
    """The bid package format (JSON Schema): the BoQ and each bidder's price schedule."""
    return Path(__file__).with_name("bid_package.schema.json").read_text()


@mcp.resource("fairprice://bid-package/example", mime_type="application/json")
def package_example() -> str:
    """A complete example package (synthetic): one line, three bidders and two planted problems."""
    return (ROOT / "examples" / "bid-package.json").read_text()


@mcp.resource("fairprice://bid-package/template.xlsx",
              mime_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
def package_template() -> bytes:
    """A blank bid package workbook: the BoQ and one sheet per bidder."""
    from fairprice import batch
    return batch.template_bytes(".xlsx")


# ---------------------------------------------------------------- the web: downloads (HTTP only)

@mcp.custom_route("/reports/{name}", methods=["GET"])
async def report_file(request: Request):
    """A report file by the signed, one-hour link that write_price_review and get_price_review give out."""
    name, q = request.path_params["name"], request.query_params
    if not (q.get("expires", "").isdigit() and int(q["expires"]) > time.time()
            and hmac.compare_digest(q.get("sig", ""), sign(name, int(q["expires"])))):
        return Response("This link is not valid or has expired.", status_code=403)
    try:
        content = storage().load(name)
    except (OSError, ValueError):
        return Response("No such report file.", status_code=404)
    return Response(content, headers={"Content-Disposition": f'attachment; filename="{name}"'})


@mcp.custom_route("/template.xlsx", methods=["GET"])
async def template_file(request: Request):
    """A blank tender workbook, for people who fill tenders in by hand."""
    return Response(package_template(), media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": 'attachment; filename="tender.xlsx"'})


def main(argv=None):
    p = argparse.ArgumentParser(description="FairPrice's MCP server.")
    p.add_argument("--http", action="store_true", help="serve over HTTP (MCP at /mcp)")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--add-key", metavar="ORG", help="create an access key for an organisation, then exit")
    p.add_argument("--name", help="the app's name, shown in the audit log")
    p.add_argument("--revoke-key", metavar="ID", help="revoke an access key by its id (the part after fp_)")
    args = p.parse_args(argv)
    if args.add_key:
        if not args.name:
            p.error("--add-key needs --name")
        key = add_key(args.add_key, args.name)
        return print(f"Access key for {args.name} ({args.add_key}). Shown once; keep it like a password:\n{key}")
    if args.revoke_key:
        return print("Revoked." if revoke_key(args.revoke_key) else "No such active key.")
    if args.http:
        mcp.settings.host, mcp.settings.port = args.host, args.port
        return mcp.run(transport="streamable-http")
    mcp.run()


if __name__ == "__main__":
    main()
