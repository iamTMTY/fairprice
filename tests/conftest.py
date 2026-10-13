"""Shared test setup: a throwaway data folder, a small synthetic OCDS dataset built through the real ingest (with the
registry, downloads and World Bank mocked), and a scripted stand-in for the model.

Settings are set before any fairprice module is imported, so nothing touches the real data/, reports/ or database.
"""

import contextlib
import gzip
import json
import os
import re
import shutil
import tempfile
from pathlib import Path
from unittest import mock

TMP = Path(tempfile.mkdtemp(prefix="fairprice-tests-"))
os.environ.update({
    "FAIRPRICE_DB": str(TMP / "fairprice.duckdb"),
    "FAIRPRICE_SQL_URL": f"sqlite:///{TMP / 'runs.sqlite'}",
    "FAIRPRICE_REPORTS": str(TMP / "reports"),
    "FAIRPRICE_STORAGE": "file",
    "FAIRPRICE_REPORT_FORMAT": "xlsx",
    "FAIRPRICE_OPEN_DRAFT": "0",
    "FAIRPRICE_MODEL": "ollama:gemma4",
})

import pytest  # noqa: E402
from langchain_core.language_models import BaseChatModel  # noqa: E402
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage  # noqa: E402
from langchain_core.outputs import ChatGeneration, ChatResult  # noqa: E402

from fairprice import ingest  # noqa: E402

ingest.RAW, ingest.MANIFEST, ingest.STATUS = TMP / "raw", TMP / "raw" / "manifest.json", TMP / "ingest-status.json"
SOURCE = TMP / "registry"  # the "downloads" the mocked registry serves


# ---------------------------------------------------------------- synthetic publications

def release(prefix, n, buyer, date, award=None, contract=None):
    doc = {"ocid": f"{prefix}-{n:04d}", "id": f"{prefix}-{n:04d}-r1", "date": date, "buyer": {"name": buyer}}
    if award:
        doc["awards"] = [award]
    if contract:
        doc["contracts"] = [contract]
    return doc


def nigeria():
    """Single-item contracts with quantities (read per contract), plus the traps the agent must handle."""
    docs, n = [], 0

    def add(description, qty, amount, year, buyer="FEDERAL MINISTRY OF WORKS", items=None):
        nonlocal n
        n += 1
        items = items or [{"id": "1", "description": description, "quantity": qty}]
        award = {"id": f"a{n}", "title": description, "date": f"{year}-05-01T00:00:00Z",
                 "value": {"amount": amount, "currency": "NGN"}, "suppliers": [{"name": "Supplier Ltd"}]}
        contract = {"id": f"c{n}", "awardID": f"a{n}", "title": description, "dateSigned": f"{year}-05-02T00:00:00Z",
                    "value": {"amount": amount, "currency": "NGN"}, "items": items}
        docs.append(release("ocds-tstng1", n, buyer, f"{year}-05-01T00:00:00Z", award, contract))

    for i, (price, year) in enumerate([(80e6, 2024), (85e6, 2024), (88e6, 2023), (90e6, 2023), (92e6, 2022),
                                       (95e6, 2022), (100e6, 2021)]):
        add(f"Supply of Toyota Hilux double cabin pickup unit {i}", 1, price, year)
    add("Supply of 2 Toyota Hilux and 1 Toyota Prado", 2, 300e6, 2023)  # a bundle
    add("Toyota Hilux double cabin pickup (placeholder date)", 1, 90e6, 2001)
    add("Toyota Hilux double cabin pickup for testing", 1, 90e6, 2024, buyer="TEST MINISTRY - NOCOPO")
    add("Supply of Toyota Hilux double cabin pickup unit 0", 1, 80e6, 2024)  # duplicate of the first
    add("Laptops and printers", None, 10e6, 2023,
        items=[{"id": "1", "description": "Laptop", "quantity": 5}, {"id": "2", "description": "Printer", "quantity": 2}])
    for i, price in enumerate([2.0e6, 2.2e6]):
        add(f"Laptop computer core i5 batch {i}", 10, price * 10, 2024)
    return docs


def kenya():
    """Award totals only: no items, no quantities."""
    return [release("ocds-tstke1", n, "COUNTY GOVERNMENT", "2023-03-01T00:00:00Z",
                    award={"id": f"a{n}", "title": f"Supply of Toyota Hilux vehicles lot {n}", "date": "2023-03-01T00:00:00Z",
                           "value": {"amount": 9e6 * n, "currency": "KES"}, "suppliers": [{"name": "Kenya Motors"}]})
            for n in range(1, 4)]


def rwanda():
    """Contract items that carry their own unit price (read per item)."""
    items = [{"id": f"i{k}", "description": "Printer or facsimile toner", "quantity": q,
              "unit": {"name": name, "value": {"amount": price, "currency": "RWF"}}}
             for k, (name, q, price) in enumerate([("Cartridge HP 80A", 10, 45000), ("Cartridge HP 80A", 5, 52000),
                                                    ("Mouse USB", 20, 4000)])]
    return [release("ocds-tstrw1", 1, "MINIJUST", "2024-02-01T00:00:00Z",
                    award={"id": "a1", "title": "Office supplies", "date": "2024-02-01T00:00:00Z",
                           "value": {"amount": 800000, "currency": "RWF"}, "suppliers": [{"name": "Kigali Office"}]},
                    contract={"id": "c1", "awardID": "a1", "title": "Office supplies", "dateSigned": "2024-02-02T00:00:00Z",
                              "value": {"amount": 800000, "currency": "RWF"}, "items": items})]


PUBLICATIONS = {147: ("ke", "Kenya: Public Procurement Regulatory Authority (PPRA)", kenya),
                64: ("ng", "Nigeria: Bureau of Public Procurement (BPP)", nigeria),
                145: ("rw", "Rwanda: Public Procurement Authority (RPPA)", rwanda),
                999: ("ng", "Nigeria: Empty State", None)}  # a registry entry with no data


def registry_html():
    return "".join(f'<article><img src="/static/img/flags/{flag}.abc.svg"><h3 class="h2">{title}</h3>'
                   f'<a href="/en/publication/{pid}" class="x">See details</a></article>'
                   for pid, (flag, title, _) in PUBLICATIONS.items())


def world_bank_json(url):
    rows = []
    for code, cpi0, fx0 in (("NG", 100, 300), ("KE", 100, 100), ("RW", 100, 900)):
        for year in range(2015, 2026):
            rows.append({"country": {"id": code}, "date": str(year), "value": (cpi0 if "CPI" in url else fx0) * 1.1 ** (year - 2015)})
    return json.dumps([{"page": 1}, rows])


def fake_fetch(url):
    if "worldbank" in url:
        return world_bank_json(url)
    return registry_html()


def fake_download(link, path):
    pid = int(re.search(r"/publication/(\d+)/", link).group(1))
    shutil.copy(SOURCE / f"{pid}.jsonl.gz", path)


@contextlib.contextmanager
def offline_registry():
    with mock.patch.object(ingest, "fetch", fake_fetch), mock.patch("urllib.request.urlretrieve", fake_download):
        yield


def write_sources():
    SOURCE.mkdir(parents=True, exist_ok=True)
    for pid, (_, _, make) in PUBLICATIONS.items():
        with gzip.open(SOURCE / f"{pid}.jsonl.gz", "wt") as f:
            for doc in make() if make else []:
                f.write(json.dumps(doc) + "\n")


@pytest.fixture(scope="session", autouse=True)
def database():
    """Build the test database once: a fresh install (Kenya and Nigeria), then Rwanda added by code."""
    write_sources()
    with offline_registry():
        ingest.main([])
        ingest.main(["--add", "RW"])
    yield ingest.DB
    shutil.rmtree(TMP, ignore_errors=True)


def ids(sql, *params):
    import duckdb
    with duckdb.connect(str(ingest.DB), read_only=True) as con:
        return [r[0] for r in con.execute(sql, params).fetchall()]


# ---------------------------------------------------------------- the scripted model

class ScriptedModel(BaseChatModel):
    """Stands in for Gemma. Plays the Analyst and the Challenger by fixed rules, so a full graph run is predictable.

    keywords: {item: [first search keywords]}. reject: description words to reject. remove: ids the Challenger removes.
    verdict: the Challenger's last line, or None for an answer without a verdict.
    """

    keywords: dict = {}
    reject: tuple = ("Prado",)
    remove: tuple = ()
    verdict: str | None = "VERDICT: DISPUTED the band mixes years."
    stop_after_search: bool = False

    def bind_tools(self, tools, **kwargs):
        return self

    @property
    def _llm_type(self):
        return "scripted"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        system = next((m.content for m in messages if isinstance(m, SystemMessage)), "")
        tools = [m for m in messages if isinstance(m, ToolMessage)]
        call = lambda name, args: AIMessage("", tool_calls=[{"name": name, "args": args, "id": f"call{len(messages)}"}])
        if "independent auditor" in system:
            if not tools:
                msg = call("execute_query", {"sql": "SELECT id, description FROM lines LIMIT 3"})
            else:
                text = "".join(f"REMOVE L{i}: a bundle of different vehicles\n" for i in self.remove)
                msg = AIMessage(text + (self.verdict or "The band looks odd."))
            return ChatResult(generations=[ChatGeneration(message=msg)])
        item = re.search(r"Item under review: (.+)", system).group(1).strip()
        last = messages[-1]
        first = self.keywords.get(item, [item.split()[0]])
        searches = sum(t.name == "find_comparables" for t in tools)
        if not any(t.name == "plan_searches" for t in tools):
            msg = call("plan_searches", {"searches": [first, [item.split()[-1]]], "notes": "reject bundles"})
        elif isinstance(last, HumanMessage) or last.name == "plan_searches":
            msg = call("find_comparables", {"keywords": first if searches == 0 else [item.split()[-1]]})
        elif last.name == "find_comparables" and not self.stop_after_search:
            from fairprice.agent import tool_text  # MCP tools return content blocks, not plain text
            found = json.loads(tool_text(last))["results"]
            reject = {str(r["id"]): "bundle" for r in found if any(w in r["description"] for w in self.reject)}
            msg = call("check_price_comparability", {"candidate_ids": [r["id"] for r in found if str(r["id"]) not in reject],
                                                     "rejected": reject})
        else:
            msg = AIMessage("I searched, kept the comparable records and checked the price [L1].")
        return ChatResult(generations=[ChatGeneration(message=msg)])


@pytest.fixture
def model():
    return ScriptedModel(keywords={"Toyota Hilux double cabin pickup": ["hilux"]})


# ---------------------------------------------------------------- the agent, wired to in-process tools

def as_tool(fn):
    """One of our MCP tools as a LangChain tool that returns JSON text, like the MCP adapter does."""
    import functools
    from langchain_core.tools import StructuredTool

    @functools.wraps(fn)
    def run(**kwargs):
        return json.dumps(fn(**kwargs), default=str)
    return StructuredTool.from_function(func=run, name=fn.__name__, description=fn.__doc__)


def execute_query(sql: str) -> str:
    """Run read-only SQL (the borrowed DuckDB server's tool)."""
    import duckdb
    with duckdb.connect(str(ingest.DB), read_only=True) as con:
        cur = con.execute(sql)
        return json.dumps([dict(zip([c[0] for c in cur.description], r)) for r in cur.fetchall()], default=str)


SAVER = None


@contextlib.asynccontextmanager
async def fake_session(model):
    """agent.session() without subprocesses: our tools in-process, the scripted model, and one shared checkpointer."""
    global SAVER
    from langchain_core.tools import StructuredTool
    from langgraph.checkpoint.memory import InMemorySaver

    from fairprice import agent, server
    SAVER = SAVER or InMemorySaver()
    tools = [as_tool(f) for f in (server.start_review, server.start_package_review, server.plan_searches,
                                  server.find_comparables, server.check_price_comparability, server.challenge_review,
                                  server.write_price_review, server.get_price_review)]
    yield agent.build_graph(tools, [StructuredTool.from_function(execute_query)], model, SAVER), {t.name: t for t in tools}


@pytest.fixture
def session(model, monkeypatch):
    """Patch agent.session with the in-process version, and return the model so a test can script it."""
    from fairprice import agent
    monkeypatch.setattr(agent, "session", lambda *a, **k: fake_session(model))
    return model


@pytest.fixture
def answers(monkeypatch):
    """Script the human at the approval gate: answers(["approve", "Ada"])."""
    def script(values):
        values = iter(values)
        monkeypatch.setattr("builtins.input", lambda prompt="": next(values))
    return script
