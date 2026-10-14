"""Load each country's OCDS publications from the OCP Data Registry, plus World Bank inflation and exchange rates.

  uv run fairprice-ingest              build the database (on a fresh install: download Kenya and Nigeria first)
  uv run fairprice-ingest --list       countries with data on the registry, and which are loaded
  uv run fairprice-ingest --add RW     add a country by ISO code: every publication it has, then rebuild

A country's publications (Nigeria has the federal BPP and its states) are downloaded unchanged and joined into
one file per country, data/raw/<country>.jsonl.gz. data/raw/manifest.json records which publications each file
holds; records are told apart by their OCID prefix, which is unique to each publisher. Each publication's
currency comes from its own data, its publisher name from the registry, and inflation and exchange rates from
the World Bank by country code. After this, the agent runs offline.
"""

import argparse
import datetime as dt
import html
import json
import os
import re
import unicodedata
import urllib.request
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parents[2]
DB = ROOT / os.environ.get("FAIRPRICE_DB", "data/fairprice.duckdb")  # same setting as the agent
RAW = ROOT / "data" / "raw"
MANIFEST = RAW / "manifest.json"
STATUS = ROOT / "data" / "ingest-status.json"
REGISTRY = "https://data.open-contracting.org"
DEFAULT_COUNTRIES = ["KE", "NG"]
# Kenya PPRA and Nigeria BPP go first, in this order: the eval cases refer to their record ids.
FIRST = [147, 64]
# Files loaded before there was a manifest: each holds exactly one publication.
LEGACY = [
    {"publication": 147, "country": "KE", "title": "Kenya: Public Procurement Regulatory Authority (PPRA)",
     "prefix": "ocds-5whusi", "file": "kenya.jsonl.gz"},
    {"publication": 64, "country": "NG", "title": "Nigeria: Bureau of Public Procurement (BPP)",
     "prefix": "ocds-gyl66f", "file": "nigeria.jsonl.gz"},
]
ITEMS_SHARE = 0.5  # ponytail: a reading is chosen when at least half the publication's contracts or items support it

TABLES_SQL = """
CREATE OR REPLACE TABLE release_info AS  -- the few fields lines need, so joins never carry the full documents
SELECT source, country, release_id, ocid,
       coalesce(nullif(trim(doc->>'$.buyer.name'), ''),  -- some publishers leave buyer.name empty and name
                nullif(trim(doc->>'$.tender.procuringEntity.name'), ''),  -- the buyer elsewhere in the record
                (SELECT nullif(trim(p->>'$.name'), '') FROM unnest(CAST(doc->'$.parties' AS JSON[])) AS t(p)
                 WHERE (p->>'$.roles') LIKE '%"buyer"%' OR (p->>'$.roles') LIKE '%"procuringEntity"%' LIMIT 1)) AS buyer
FROM releases;

CREATE OR REPLACE TABLE awards AS
SELECT r.source, r.country, r.release_id, a->>'$.id' AS award_id,
       a->>'$.title' AS title, a->>'$.date' AS award_date,
       coalesce(a->>'$.suppliers[0].name', a->>'$.suppliers[0].id') AS supplier,
       TRY_CAST(a->>'$.value.amount' AS DOUBLE) AS amount, a->>'$.value.currency' AS currency
FROM releases r, unnest(json_extract(r.doc, '$.awards[*]')) AS t(a);

CREATE OR REPLACE TABLE contracts AS
SELECT r.source, r.country, r.release_id, c->>'$.id' AS contract_id, c->>'$.awardID' AS award_id,
       c->>'$.title' AS title, c->>'$.dateSigned' AS date_signed,
       coalesce(json_array_length(c->'$.items'), 0) AS n_items,
       c->>'$.items[0].description' AS item_description,
       TRY_CAST(c->>'$.items[0].quantity' AS DOUBLE) AS item_quantity,
       TRY_CAST(c->>'$.value.amount' AS DOUBLE) AS amount, c->>'$.value.currency' AS currency,
       c->'$.items' AS items
FROM releases r, unnest(json_extract(r.doc, '$.contracts[*]')) AS t(c);

CREATE OR REPLACE TABLE lines (id BIGINT, country VARCHAR, release_id VARCHAR, line_id VARCHAR, line_type VARCHAR,
  description VARCHAR, title VARCHAR, n_items BIGINT, quantity DOUBLE, amount DOUBLE, currency VARCHAR,
  raw_date VARCHAR, supplier VARCHAR, ocid VARCHAR, buyer VARCHAR, year BIGINT, unit_price DOUBLE);
"""

# One row per priced line, read in one of three ways depending on what the publication contains:
#   item_prices  each contract item has its own unit price (e.g. Rwanda): one line per priced item
#   items        contracts list item quantities (e.g. Nigeria): one line per contract, priced if it has one item
#   awards       no quantities at all (e.g. Kenya): one line per award, with no unit price
LINES = {
    "item_prices": """
  SELECT c.country, c.release_id, c.contract_id || '/' || (i->>'$.id') AS line_id, 'item' AS line_type,
         CASE WHEN (i->>'$.unit.name') IS NOT NULL AND (i->>'$.description') IS NOT NULL
                   AND (i->>'$.unit.name') <> (i->>'$.description')
              THEN (i->>'$.unit.name') || ' (' || (i->>'$.description') || ')'
              ELSE coalesce(i->>'$.unit.name', i->>'$.description') END AS description,
         c.title, 1 AS n_items, TRY_CAST(i->>'$.quantity' AS DOUBLE) AS quantity,
         TRY_CAST(i->>'$.quantity' AS DOUBLE) * TRY_CAST(i->>'$.unit.value.amount' AS DOUBLE) AS amount,
         coalesce(i->>'$.unit.value.currency', c.currency) AS currency,
         coalesce(c.date_signed, a.award_date) AS raw_date, a.supplier
  FROM contracts c CROSS JOIN unnest(json_extract(c.items, '$[*]')) AS t(i)
  LEFT JOIN awards a ON a.source = c.source AND a.country = c.country AND a.release_id = c.release_id
                    AND a.award_id = c.award_id
  WHERE c.source = $source AND TRY_CAST(i->>'$.unit.value.amount' AS DOUBLE) > 0""",
    "items": """
  SELECT c.country, c.release_id, c.contract_id AS line_id, 'contract' AS line_type,
         CASE WHEN c.n_items = 1 THEN c.item_description ELSE c.title END AS description,
         c.title, c.n_items,
         CASE WHEN c.n_items = 1 THEN c.item_quantity END AS quantity,
         c.amount, c.currency, coalesce(c.date_signed, a.award_date) AS raw_date, a.supplier
  FROM contracts c LEFT JOIN awards a USING (source, country, release_id, award_id)
  WHERE c.source = $source""",
    "awards": """
  SELECT a.country, a.release_id, a.award_id AS line_id, 'award' AS line_type,
         a.title AS description, a.title, 0 AS n_items, NULL::DOUBLE AS quantity,
         a.amount, a.currency, coalesce(c.date_signed, a.award_date) AS raw_date, a.supplier
  FROM awards a LEFT JOIN contracts c USING (source, country, release_id, award_id)
  WHERE a.source = $source""",
}
INSERT_LINES = """
INSERT INTO lines
SELECT $offset + row_number() OVER (ORDER BY x.release_id, x.line_id) AS id, x.*, r.ocid, r.buyer,
       year(TRY_CAST(raw_date AS TIMESTAMPTZ)) AS year,
       CASE WHEN quantity > 0 AND amount > 0 THEN amount / quantity END AS unit_price
FROM ({select}) x JOIN release_info r ON r.source = $source AND r.country = x.country AND r.release_id = x.release_id
"""


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": "FairPrice ingest"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read().decode()


def registry():
    """Every publication on the registry: [{publication, country (ISO code), title}], read from its search page."""
    found = []
    for article in re.findall(r"<article.*?</article>", fetch(f"{REGISTRY}/en/search/"), re.S):
        pub, flag = re.search(r'/en/publication/(\d+)"', article), re.search(r"/flags/([a-z-]+)\.", article)
        title = re.search(r"<h3[^>]*>(.*?)</h3>", article, re.S)
        if pub and flag and title:
            found.append({"publication": int(pub.group(1)), "country": flag.group(1).upper(),
                          "title": html.unescape(title.group(1)).strip()})
    if not found:
        raise SystemExit("Could not read the publication list from the registry; its page layout may have changed.")
    return found


def countries(pubs):
    """{code: {"code", "name", "publications"}} from the registry list."""
    out = {}
    for p in pubs:
        entry = out.setdefault(p["country"], {"code": p["country"], "name": p["title"].split(":")[0].strip(),
                                              "publications": 0})
        entry["publications"] += 1
    return out


def file_for(name):
    """'Côte d'Ivoire' -> 'cote-d-ivoire.jsonl.gz'."""
    ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", ascii_name.lower()).strip("-") + ".jsonl.gz"


def url(pub):
    return f"{REGISTRY}/en/publication/{pub['publication']}"


def manifest():
    """The publications downloaded so far, in load order."""
    if MANIFEST.exists():
        return json.loads(MANIFEST.read_text())
    return [p for p in LEGACY if (RAW / p["file"]).exists()]


def save_manifest(pubs):
    RAW.mkdir(parents=True, exist_ok=True)
    MANIFEST.write_text(json.dumps(pubs, indent=2, ensure_ascii=False) + "\n")


def status(**update):
    """Read the background ingest status, or update it."""
    current = json.loads(STATUS.read_text()) if STATUS.exists() else {}
    if update:
        current = {**current, **update}
        STATUS.parent.mkdir(parents=True, exist_ok=True)
        STATUS.write_text(json.dumps(current, indent=2) + "\n")
    return current


def append(pub, file):
    """Download one publication and join it onto its country's file. Returns its OCID prefix, or None if empty."""
    RAW.mkdir(parents=True, exist_ok=True)
    part = RAW / f"{pub['publication']}.part"
    link = f"{url(pub)}/download?name=full.jsonl.gz"
    print(f"Downloading {pub['title']} from {link}", flush=True)
    urllib.request.urlretrieve(link, part)
    try:
        if part.stat().st_size < 100:  # some registry entries have no full download
            print(f"  skipped: no data in {pub['title']}")
            return None
        with duckdb.connect() as con:
            prefix = con.execute("SELECT regexp_extract(json->>'$.ocid', '^(ocds-[a-z0-9]+)') FROM "
                                 "read_ndjson_objects(?, compression = 'gzip') GROUP BY 1 ORDER BY count(*) DESC LIMIT 1",
                                 [str(part)]).fetchone()[0]
        target = RAW / file
        size = target.stat().st_size if target.exists() else 0
        with target.open("ab") as out:
            try:
                out.write(part.read_bytes())  # gzip files joined end to end read back as one
            except BaseException:
                out.truncate(size)  # never leave a half-written country file
                raise
        return prefix
    finally:
        part.unlink(missing_ok=True)


def add(codes):
    """Download every publication of these countries that is not loaded yet, then rebuild the database."""
    pubs = registry()
    names = countries(pubs)
    wanted = {c.upper() for c in codes}
    if unknown := sorted(wanted - names.keys()):
        raise SystemExit(f"No data on the registry for {', '.join(unknown)}. See `fairprice-ingest --list`.")
    loaded = manifest()
    have = {p["publication"] for p in loaded}
    new = [p for p in pubs if p["country"] in wanted and p["publication"] not in have]
    new.sort(key=lambda p: (p["publication"] not in FIRST,
                            FIRST.index(p["publication"]) if p["publication"] in FIRST else p["publication"]))
    for p in new:
        file = file_for(names[p["country"]]["name"])
        if prefix := append(p, file):
            loaded.append({**p, "prefix": prefix, "file": file})
            save_manifest(loaded)  # after each file, so the manifest always matches what is on disk
    build(loaded)


def build(pubs):
    if not pubs:
        raise SystemExit("No data downloaded yet. Run `fairprice-ingest` or `fairprice-ingest --add <country code>`.")
    print("Building the database from: " + "; ".join(p["title"] for p in pubs), flush=True)
    DB.parent.mkdir(exist_ok=True)
    tmp = DB.with_suffix(".building")  # renamed at the end, so an interrupted build is never mistaken for a finished one
    tmp.unlink(missing_ok=True)
    con = duckdb.connect(str(tmp))
    con.execute("CREATE TEMP TABLE prefixes (prefix VARCHAR, url VARCHAR)")
    con.executemany("INSERT INTO prefixes VALUES (?, ?)", [(p["prefix"], url(p)) for p in pubs])
    con.execute("CREATE TABLE releases (country VARCHAR, ocid VARCHAR, release_id VARCHAR, doc JSON, source VARCHAR)")
    for file, country in dict.fromkeys((p["file"], p["country"]) for p in pubs):
        con.execute("INSERT INTO releases SELECT ?, json->>'$.ocid', json->>'$.id', json, p.url "
                    "FROM read_ndjson_objects(?) LEFT JOIN prefixes p "
                    "ON p.prefix = regexp_extract(json->>'$.ocid', '^(ocds-[a-z0-9]+)')", [country, str(RAW / file)])
    if stray := con.execute("SELECT count(*) FROM releases WHERE source IS NULL").fetchone()[0]:
        print(f"  {stray:,} records have an OCID prefix that matches no publication and are left out.")
    con.execute(TABLES_SQL)
    con.execute("CREATE TABLE sources (position INTEGER, publication INTEGER, country VARCHAR, title VARCHAR, "
                "prefix VARCHAR, url VARCHAR, currency VARCHAR, priced_per VARCHAR, first_id BIGINT, last_id BIGINT)")
    for position, p in enumerate(pubs):
        priced, items = con.execute("SELECT count(*) FILTER (WHERE TRY_CAST(i->>'$.unit.value.amount' AS DOUBLE) > 0), "
                                    "count(*) FROM contracts, unnest(json_extract(items, '$[*]')) AS t(i) "
                                    "WHERE source = ?", [url(p)]).fetchone()
        with_qty, total = con.execute("SELECT count(*) FILTER (WHERE item_quantity > 0), count(*) FROM contracts "
                                      "WHERE source = ?", [url(p)]).fetchone()
        kind = ("item_prices" if items and priced / items >= ITEMS_SHARE
                else "items" if total and with_qty / total >= ITEMS_SHARE else "awards")
        offset = con.execute("SELECT coalesce(max(id), 0) FROM lines").fetchone()[0]
        con.execute(INSERT_LINES.format(select=LINES[kind]), {"offset": offset, "source": url(p)})
        last = con.execute("SELECT coalesce(max(id), 0) FROM lines").fetchone()[0]
        currency = con.execute("SELECT currency FROM awards WHERE source = ? AND currency IS NOT NULL "
                               "GROUP BY 1 ORDER BY count(*) DESC LIMIT 1", [url(p)]).fetchone()
        con.execute("INSERT INTO sources VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    [position, p["publication"], p["country"], p["title"], p["prefix"], url(p),
                     currency[0] if currency else None, kind, offset + 1, last])
    codes = sorted({p["country"] for p in pubs})
    for table, indicator in (("cpi", "FP.CPI.TOTL"), ("fx", "PA.NUS.FCRF")):
        con.execute(f"CREATE TABLE {table} (country VARCHAR, year INTEGER, value DOUBLE)")
        con.executemany(f"INSERT INTO {table} VALUES (?, ?, ?)", world_bank(indicator, codes))
    for title, cur, kind, n, priced in con.execute(
            "SELECT s.title, s.currency, s.priced_per, count(l.id), count(l.unit_price) FROM sources s "
            "LEFT JOIN lines l ON l.id BETWEEN s.first_id AND s.last_id "
            "GROUP BY s.position, 1, 2, 3 ORDER BY s.position").fetchall():
        print(f"  {title}: {cur}, {n:,} records, {priced:,} with a price per item"
              + (" (no quantities published)" if kind == "awards" else ""))
    if missing := set(codes) - {r[0] for r in con.execute("SELECT DISTINCT country FROM cpi").fetchall()}:
        print(f"  No World Bank inflation data for {', '.join(sorted(missing))}: prices there cannot be adjusted.")
    con.close()
    tmp.replace(DB)


def world_bank(indicator, codes):
    link = (f"https://api.worldbank.org/v2/country/{';'.join(codes)}/indicator/{indicator}"
            "?format=json&per_page=2000&date=2010:2030")
    rows = json.loads(fetch(link))[1] or []
    return [(row["country"]["id"], int(row["date"]), row["value"]) for row in rows if row["value"] is not None]


def main(argv=None):
    p = argparse.ArgumentParser(prog="fairprice-ingest", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--list", action="store_true", help="list countries with data on the registry")
    p.add_argument("--add", metavar="CODE", help="ISO country code to add, e.g. RW")
    args = p.parse_args(argv)
    if args.list:
        have = {p["country"] for p in manifest()}
        for c in sorted(countries(registry()).values(), key=lambda c: c["code"]):
            print(f"{c['code']:<4} {c['name']:<34} {c['publications']:>2} publication(s)"
                  + ("  (loaded)" if c["code"] in have else ""))
        return
    codes = [args.add] if args.add else ([] if manifest() else DEFAULT_COUNTRIES)
    status(state="running", countries=codes or "rebuild", started=dt.datetime.now().isoformat(timespec="seconds"),
           finished=None, message=None, pid=os.getpid())
    try:
        add(codes) if codes else build(manifest())
    except BaseException as e:
        status(state="failed", finished=dt.datetime.now().isoformat(timespec="seconds"), message=str(e)[:300])
        raise
    status(state="done", finished=dt.datetime.now().isoformat(timespec="seconds"),
           message="loaded: " + ", ".join(sorted({p["country"] for p in manifest()})))


if __name__ == "__main__":
    main()
