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

from fairprice.db import REPORTS, engine, request_approval, use_approval
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


@mcp.tool()
def find_comparables(keywords: list[str], country: str | None = None, year_from: int | None = None,
                     year_to: int | None = None, limit: int = 25) -> dict:
    """Search published contract awards whose description contains ALL keywords (case-insensitive).

    Use short, specific keywords (e.g. ["probook", "laptop"]). If too few results come back,
    retry with fewer or alternative keywords. Each result carries data-quality flags.
    limit: how many records to return, at most 50; total_matches always gives the full count.
    country: ISO code of a loaded country, e.g. "NG". Some countries publish award totals only, with no
    quantities, so their records have no unit price.
    """
    if not keywords:
        return {"error": "Give at least one keyword."}
    where = ["(description || ' ' || coalesce(title, '')) ILIKE ?" for _ in keywords]
    params = [f"%{k}%" for k in keywords]
    for clause, value in (("country = ?", country), ("year >= ?", year_from), ("year <= ?", year_to)):
        if value is not None:
            where.append(clause)
            params.append(value)
    sql = f"FROM lines WHERE {' AND '.join(where)}"
    total = rows(f"SELECT count(*) AS n {sql}", params)[0]["n"]
    found = rows(
        f"SELECT id, country, year, quantity, unit_price, amount, currency, n_items, line_type, description, buyer {sql} "
        f"ORDER BY year DESC NULLS LAST LIMIT ?",
        [*params, min(limit, 50)],
    )
    for r in found:
        r["flags"] = data_flags(r)
        r["description"] = r["description"][:140] if r["description"] else None
        r["buyer"] = r["buyer"][:60] if r["buyer"] else None
        for k in ("unit_price", "amount"):
            r[k] = round(r[k], 2) if r[k] is not None else None
        del r["n_items"], r["line_type"]  # used for the flags only; keep the output the model sees unchanged
    return {"total_matches": total, "returned": len(found), "results": found}


@mcp.tool()
def check_price_comparability(unit_price: float, currency: str, year: int, candidate_ids: list[int],
                              rejected: dict[str, str] | None = None) -> dict:
    """Build a price band for the price under review from candidate records.

    candidate_ids: record ids from find_comparables that you believe describe the same product.
    rejected: {id: reason} for candidates YOU judge not comparable (different model, spec, pack size).
    The tool also rejects records with invalid dates, no unit price, multi-item contracts and
    duplicates, then adjusts every price to `year` and `currency` using World Bank CPI and FX.
    """
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


@mcp.tool()
def write_price_review(report: str, approval: str = "") -> dict:
    """Approve a draft price review and write its final files. A person must approve it first.

    report: the draft's name (DRAFT-...), as shown when the draft was made.
    approval: the id of a person's approval for this report. Without one, this records a request and replies with
    the command a person runs in the terminal to approve it (`fairprice --approve <id>`); the approver's name comes
    from that person, not from this call. The review is refused if it cites no records or records that do not exist.
    """
    from fairprice import report as rep  # report imports this module

    with engine().connect() as conn:
        row = conn.execute(select(REPORTS).where(REPORTS.c.name == report, REPORTS.c.status == "draft")).first()
    if row is None:
        return {"error": f"Refused: {report!r} is not a draft report."}
    data, cited = row.data, sorted(set(rep.cited(row.data)))
    if not cited:
        return {"error": "Refused: the report cites no source records."}
    known = {r["id"] for r in rows(f"SELECT id FROM lines WHERE id IN ({','.join('?' * len(cited))})", cited)}
    if missing := sorted(set(cited) - known):
        return {"error": f"Refused: cited records do not exist: {missing}"}
    if not approval:
        request = request_approval("write_price_review", report)
        return {"status": "needs_approval", "request": request,
                "message": f"A person must approve this report. In the terminal: uv run fairprice --approve {request}"}
    granted = use_approval(approval, "write_price_review", report)
    if granted is None:
        return {"error": "Refused: that is not an unused human approval for this report."}
    now = dt.datetime.now().astimezone()
    data["report"].update(status="approved", approved_by=granted.approved_by, approved_at=now.isoformat(timespec="seconds"))
    name = f"{now:%Y%m%d-%H%M%S}-{report.removeprefix('DRAFT-')}"
    store, written = storage(), rep.files(data, name, row.format)
    for file, content in written.items():
        store.save(file, content)
    for file in row.files or []:
        store.delete(file)
    with engine().begin() as conn:
        conn.execute(REPORTS.update().where(REPORTS.c.id == row.id).values(
            name=name, status="approved", approved_by=granted.approved_by, approved_at=now, files=list(written), data=data))
    return {"saved": name, "approved_by": granted.approved_by, "files": [store.location(f) for f in written],
            "sources": len(cited)}


@mcp.tool()
def get_price_review(name: str = "") -> dict:
    """Retrieve an approved price review as JSON. Without a name, list the approved reviews, newest first.

    With a name: the report's summary and approval, the price of every line and bidder, the past purchases it
    cites (with their official record ids), and where its files are (paths, or links valid for one hour in S3).
    """
    with engine().connect() as db:
        if not name:
            found = db.execute(select(REPORTS.c.name, REPORTS.c.approved_by, REPORTS.c.approved_at, REPORTS.c.format)
                               .where(REPORTS.c.status == "approved").order_by(REPORTS.c.approved_at.desc()).limit(50)).all()
            return {"reports": [{"name": r.name, "approved_by": r.approved_by, "format": r.format,
                                 "approved_at": r.approved_at.isoformat() if r.approved_at else None} for r in found]}
        row = db.execute(select(REPORTS).where(REPORTS.c.name == name, REPORTS.c.status == "approved")).first()
    if row is None:
        return {"error": f"No approved report named {name!r}. Call get_price_review without a name to list them."}
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
