"""FairPrice agent: Analyst -> Challenger -> re-check -> draft -> human approval -> save.

Usage:
  uv run fairprice "HP ProBook 440 G8 laptop" --unit-price 1450000 --year 2025
"""

import argparse
import asyncio
import contextlib
import datetime as dt
import importlib.util
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import TypedDict

import duckdb
from langchain.agents import create_agent
from langchain.chat_models import init_chat_model
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.tools import StructuredTool
from langchain_mcp_adapters.client import MultiServerMCPClient
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.errors import GraphRecursionError
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt
from sqlalchemy import select

from fairprice import ingest, report
from fairprice.db import AUDIT_LOG, REPORTS, add_step, approvals, decide, engine
from fairprice.storage import storage
from fairprice.server import MIN_COMPARABLES, discard_review, record_id

ROOT = Path(__file__).resolve().parents[2]
DB = str(ROOT / os.environ.get("FAIRPRICE_DB", "data/fairprice.duckdb"))  # relative paths are relative to ROOT
MODEL = os.environ.get("FAIRPRICE_MODEL", "ollama:gemma4")  # chosen over qwen3:8b by EVALS.md
STEP_BUDGET = 30  # max graph steps per sub-agent before it must stop and report "unresolved"

BORROWED = "mcp-server-motherduck==1.1.0"  # needs MCP SDK 2.x, our adapter needs 1.x, so it runs in its own uvx env


def borrowed_args():
    """uvx checks PyPI each time it starts a server, and a network blip then fails the run.
    Once the server is cached, start it offline; only the very first run goes online."""
    cached = subprocess.run(["uvx", "--offline", BORROWED, "--help"], capture_output=True).returncode == 0
    return (["--offline"] if cached else []) + [BORROWED, "--db-path", DB, "--max-rows", "100", "--ephemeral-connections"]


def servers():
    return {
        "fairprice": {"command": sys.executable, "args": ["-m", "fairprice.server"], "transport": "stdio",
                      "env": {**os.environ, "FAIRPRICE_DB": DB}},
        # Borrowed: MotherDuck's DuckDB MCP server, read-only by default for local files.
        "duckdb": {"command": "uvx", "args": borrowed_args(), "transport": "stdio",
                   "env": {**os.environ, "FASTMCP_SHOW_SERVER_BANNER": "false", "FASTMCP_LOG_LEVEL": "WARNING"}},
    }

ANALYST = """You prepare price evidence for a public procurement evaluation committee.
You never recommend a winner, never decide an award, and never accuse a supplier.

Item under review: {item}
Price under review: {unit_price} {currency} per unit, {year}. Country of the data to search: {country}.
{feedback}
Steps:
1. Call plan_searches first. List the keyword sets you will try, most specific first, each built on the word
   that names the product's model or kind (for example [["hilux"], ["hilux", "pickup"], ["pickup", "4x4"]]),
   and say in notes what you will reject. A keyword matches its words in any order.
2. Call find_comparables with the first set in your plan.
3. Read the results. Keep only records for the SAME kind of product at a comparable specification.
   Reject a record, with a short reason, if it is a different product, a bundle of mixed items,
   or a lot price (quantity 1 but the description says several units, a project, or installation works).
   Most records do not state every specification. A record that does not mention a spec (engine size,
   memory, model year) is NOT a mismatch; reject only records that state a different spec.
4. Call check_price_comparability with candidate_ids (every id you kept) and rejected ({{id: reason}}).
5. If a search found nothing, or the assessment starts with insufficient_comparables, run the next search in
   your plan, then check again with every id you kept from ALL searches. At most 4 searches in total.
6. Finish with 3-5 plain sentences: what you searched, what you rejected and why, and the assessment.
   Do not repeat prices or band figures; the report shows them."""

CHALLENGER = """You are an independent auditor. Your job is to try to DISPROVE the analyst's price finding.

Item under review: {item}. Price under review: {unit_price} {currency} per unit ({year}).
Analyst's assessment: {assessment}
Price band built from these records (prices adjusted to {year} {currency}):
{records}

Use the execute_query tool to run read-only SQL against DuckDB table `lines`
(columns: id, country, year, description, title, quantity, amount, unit_price, currency, buyer, ocid, n_items).
Prices in the table are as published; there is no inflation-adjusted column.
Remove a record ONLY if its description shows one of these:
- it is not the item under review (a different brand, model or kind of product);
- it is a bundle of several different products;
- the price clearly covers many units although quantity is 1 (a lot price, e.g. "supply of laptops", qty 1).
Never remove a record because its price is high or low, because its quantity is large (unit_price is
already per unit), or because its description is short. Removing records for their price is cherry-picking. Run at most 4 queries.

Keep your final answer under 15 lines. End it with these lines exactly:
REMOVE L<id>: <reason>    (one line per record that should not be in the band; omit if none)
VERDICT: STANDS or VERDICT: DISPUTED, followed by one sentence explaining why.
STANDS means the remaining records are a fair benchmark for this item. DISPUTED means they are not."""


class State(TypedDict, total=False):
    request: dict
    feedback: str
    analysis: dict  # last check_price_comparability result from the Analyst
    rejected: dict
    searches: list
    challenge: dict
    final: dict  # band after the Challenger's removals
    report: str
    decision: dict
    saved: str


def audit(run_id, **event):
    """Append one row to the audit log. A row without an `event` name is a tool call."""
    with engine().begin() as db:
        db.execute(AUDIT_LOG.insert().values(ts=dt.datetime.now(dt.timezone.utc), run_id=run_id,
                                             event=event.pop("event", "tool_call"), detail=event))


def show_audit(run_id):
    """Print a run's audit log, oldest first. A batch id also shows every line's run."""
    with engine().connect() as db:
        found = db.execute(select(AUDIT_LOG).where(AUDIT_LOG.c.run_id.like(f"{run_id}%")).order_by(AUDIT_LOG.c.id)).all()
    for r in found:
        d = dict(r.detail or {})
        what = f"{d.pop('server')}.{d.pop('tool')}" if "tool" in d else ""
        out = d.pop("output", None)
        print(f"{r.ts:%Y-%m-%d %H:%M:%S}  {r.run_id:<16} {r.event:<16} {what} {json.dumps(d, default=str)[:400]}")
        if out:
            try:
                out = json.dumps(json.loads(out), ensure_ascii=False)  # one line, not the tool's indented JSON
            except ValueError:
                pass
            print(f"{'':>53}-> {out[:400]}")
    print(f"{len(found)} entries" if found else f"No audit entries for run {run_id}.")


def tool_text(result):
    """Plain text of an MCP tool result, whatever shape the adapter returns."""
    content = getattr(result, "content", result)
    if isinstance(content, list):
        return "".join(c.get("text", "") if isinstance(c, dict) else getattr(c, "text", str(c)) for c in content)
    return content if isinstance(content, str) else json.dumps(content, default=str)


def as_json(text):
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return {"error": text}


CURRENT_RUN = None  # one run per process


async def audit_interceptor(request, handler):
    """Every MCP tool call, on every server, is shown on screen and logged with inputs and outputs."""
    print(f"  -> {request.server_name}.{request.name}({json.dumps(request.args, default=str)[:160]})", flush=True)
    start = time.perf_counter()
    try:
        result = await handler(request)
    except Exception as e:
        audit(CURRENT_RUN, server=request.server_name, tool=request.name, args=request.args, error=repr(e))
        raise
    run_id = CURRENT_RUN
    if request.name in ("start_review", "start_package_review"):  # the call that creates the run is logged under it
        run_id = as_json(tool_text(result)).get("run_id") or run_id
    audit(run_id, server=request.server_name, tool=request.name, args=request.args,
          output=tool_text(result), seconds=round(time.perf_counter() - start, 2))
    return result


def planned(messages):
    """The keyword sets of the Analyst's plan, from its plan_searches call."""
    calls = [c for m in messages if isinstance(m, AIMessage) for c in m.tool_calls if c["name"] == "plan_searches"]
    return [list(keys) for keys in (calls[-1]["args"].get("searches") or [])] if calls else []


def next_planned(messages):
    """The first planned keyword set not searched yet (keywords match their words in any order)."""
    words = lambda keys: frozenset(w.lower() for k in keys for w in str(k).split())
    tried = {words(c["args"].get("keywords") or []) for m in messages if isinstance(m, AIMessage)
             for c in m.tool_calls if c["name"] == "find_comparables"}
    return next((keys for keys in planned(messages) if words(keys) not in tried), None)


def unfinished(messages):
    """Return an instruction if the Analyst stopped too early, else None."""
    tool_msgs = [m for m in messages if isinstance(m, ToolMessage)]
    searches = [m for m in tool_msgs if m.name == "find_comparables"]
    then = " Then call check_price_comparability with every id you kept from ALL searches."
    if not any(m.name == "plan_searches" for m in tool_msgs):
        return ("Start by recording your search plan with plan_searches: the keyword sets you will try, most "
                "specific first, each built on the word that names the product's model or kind.")
    nxt = next_planned(messages)
    if searches and len(searches) < 4 and all(as_json(tool_text(m)).get("total_matches", 0) == 0 for m in searches):
        return ("Every search so far found nothing. " + (f"Run the next search in your plan: {nxt}." if nxt else
                "Search for the single word that names the product, such as 'hilux' or 'laptop'.") + then)
    if not tool_msgs or tool_msgs[-1].name != "check_price_comparability":
        return ("You searched after your last price check (or never checked). Call "
                "check_price_comparability now with every id you kept from ALL searches.")
    last = as_json(tool_text(tool_msgs[-1]))
    if (last.get("band") or {}).get("n", 0) < 10 and len(searches) < 2 and not last.get("assessment", "").startswith("insufficient"):
        return ("Your band rests on few records from a single search. " + (
            f"Run the next search in your plan ({nxt}) to confirm it." if nxt else
            "Run one broader search with just the word that names the product's model (such as 'hilux' or "
            "'probook'), never the brand alone (such as 'toyota' or 'hp') and never a specification, to confirm it.") + then)
    if last.get("assessment", "").startswith("insufficient") and len(searches) < 4:
        return ("Too few comparables. " + (f"Run the next search in your plan: {nxt}." if nxt else
                "Search again with fewer keywords: keep the one word that names the product (the brand, model or "
                "kind of product, such as 'hilux', 'probook' or 'motorcycle'), never a specification such as '125cc', "
                "'16GB' or '1.5HP', and drop generic words such as 'supply'.") + " Reject anything that is not that "
                "product; a record that does not state the specification is not a mismatch." + then)
    return None


def bound(tool, run_id, line):
    """One of our MCP tools with run_id and line filled in by code, so the model never handles them."""
    schema = tool.args_schema if isinstance(tool.args_schema, dict) else tool.args_schema.model_json_schema()
    schema = {**schema, "properties": {k: v for k, v in schema.get("properties", {}).items() if k not in ("run_id", "line")},
              "required": [r for r in schema.get("required", []) if r not in ("run_id", "line")]}

    async def call(**kwargs):
        return await tool.ainvoke({**kwargs, "run_id": run_id, "line": line})
    return StructuredTool(name=tool.name, description=tool.description, args_schema=schema, coroutine=call)


def build_graph(own_tools, borrowed_tools, model, checkpointer):
    by_name = {t.name: t for t in own_tools}
    challenger_tools = borrowed_tools  # independent read-only SQL, not the Analyst's tools
    run_tool = lambda name, req: bound(by_name[name], req["run_id"], req["line"])

    async def analyst(state: State):
        req = state["request"]
        feedback = f"Reviewer feedback to address: {state['feedback']}\n" if state.get("feedback") else ""
        tools = [run_tool(n, req) for n in ("plan_searches", "find_comparables", "check_price_comparability")]
        agent = create_agent(model, tools, system_prompt=ANALYST.format(**req, feedback=feedback))
        messages = [("user", "Prepare the price evidence.")]
        for _ in range(3):  # self-check after each attempt; send the Analyst back if work is unfinished
            try:
                messages = (await agent.ainvoke({"messages": messages}, {"recursion_limit": STEP_BUDGET}))["messages"]
            except GraphRecursionError:
                break
            if nudge := unfinished(messages):
                print(f"  ! self-check: {nudge}", flush=True)
                messages.append(("user", nudge))
            else:
                break
        calls = {c["id"]: c for m in messages if isinstance(m, AIMessage) for c in m.tool_calls}
        kept, rejected, searches, checks = set(), {}, [], 0
        for m in messages:
            if not isinstance(m, ToolMessage):
                continue
            args, out = calls.get(m.tool_call_id, {}).get("args", {}), as_json(tool_text(m))
            if m.name == "find_comparables":
                searches.append({"keywords": args.get("keywords"), "country": req["country"],
                                 "matches": out.get("total_matches")})
            elif m.name == "check_price_comparability" and "error" not in out:
                checks += 1
                kept |= {record_id(i) for i in args.get("candidate_ids") or []}
                rejected |= {str(record_id(k)): v for k, v in (args.get("rejected") or {}).items()}
        if not checks:
            return {"analysis": {"assessment": "unresolved: the analyst did not build a price band",
                                 "band": None, "accepted": [], "excluded": []}, "searches": searches}
        # The model nominates records across several checks; one final check covers all of them.
        kept -= {int(k) for k in rejected}  # keys already normalised above
        result = await run_tool("check_price_comparability", req).ainvoke(
            {"candidate_ids": sorted(kept) or [int(k) for k in rejected], "rejected": rejected})
        return {"analysis": as_json(tool_text(result)), "rejected": rejected, "searches": searches}

    async def challenger(state: State):
        a, req = state["analysis"], state["request"]
        if not a.get("band"):
            return {"challenge": {"verdict": "NOT RUN", "reason": "no price band to challenge", "remove": {}},
                    "final": a}
        records = "\n".join(f"[L{r['id']}] {r['year']} qty {r['quantity']} unit_price {r['published_unit_price']} "
                            f"(in {req['year']} prices: {r['adjusted_unit_price']}) | {r['description']}"
                            for r in a["accepted"])
        agent = create_agent(model, challenger_tools,
                             system_prompt=CHALLENGER.format(**req, assessment=a["assessment"], records=records))
        try:
            out = await agent.ainvoke({"messages": [("user", "Challenge the finding.")]},
                                      {"recursion_limit": STEP_BUDGET})
            text = out["messages"][-1].content
        except GraphRecursionError:
            text = "VERDICT: DISPUTED step budget exhausted before the challenge finished."
        audit(CURRENT_RUN, event="challenger_answer", text=text)
        accepted_ids = {r["id"] for r in a["accepted"]}
        remove = {int(i): why.strip() for i, why in re.findall(r"REMOVE\s+\[?L(\d+)\]?\s*:\s*(.+)", text)
                  if int(i) in accepted_ids}
        verdict = re.search(r"VERDICT:\s*(STANDS|DISPUTED)\W*(.*)", text)
        challenge = {"verdict": verdict.group(1) if verdict else "DISPUTED",
                     "reason": verdict.group(2).strip() if verdict else "Challenger gave no clear verdict.",
                     "remove": remove}
        await run_tool("challenge_review", req).ainvoke(
            {"verdict": challenge["verdict"], "reason": challenge["reason"],
             "objections": {str(k): v for k, v in remove.items()}})
        return {"challenge": challenge}

    async def recheck(state: State):
        remove = state["challenge"].get("remove") or {}
        if not remove:
            return {"final": state["analysis"]}
        a, req = state["analysis"], state["request"]
        if len(a["accepted"]) - len(remove) < MIN_COMPARABLES:
            # The agents disagree about most of the evidence: show the dispute to the committee
            # rather than let one model's removals empty the band.
            challenge = {**state["challenge"], "verdict": "DISPUTED", "applied": False}
            return {"final": a, "challenge": challenge}
        ids = [r["id"] for r in a["accepted"]] + [e["id"] for e in a["excluded"]]
        rejected = {**state.get("rejected", {}), **{str(k): f"Challenger: {v}" for k, v in remove.items()}}
        result = await run_tool("check_price_comparability", req).ainvoke({"candidate_ids": ids, "rejected": rejected})
        return {"final": as_json(tool_text(result))}

    async def draft(state: State):
        return {"report": report.summary(*report.single(state), None, state["request"])}

    async def approve(state: State):
        decision = interrupt({"report": state["report"]})
        return {"decision": decision, "feedback": decision.get("feedback", "")}

    def route(state: State):
        return {"approve": "save", "revise": "analyst"}.get(state["decision"]["action"], END)

    async def save(state: State):
        result = await by_name["write_price_review"].ainvoke(
            {"run_id": state["request"]["run_id"], "approval": state["decision"]["approval"]})
        return {"saved": tool_text(result)}

    g = StateGraph(State)
    for name, fn in (("analyst", analyst), ("challenger", challenger), ("recheck", recheck),
                     ("draft", draft), ("approve", approve), ("save", save)):
        g.add_node(name, fn)
    g.add_edge(START, "analyst")
    g.add_edge("analyst", "challenger")
    g.add_edge("challenger", "recheck")
    g.add_edge("recheck", "draft")
    g.add_edge("draft", "approve")
    g.add_conditional_edges("approve", route, ["save", "analyst", END])
    g.add_edge("save", END)
    return g.compile(checkpointer=checkpointer)


def open_draft(where):
    """Open the draft in Excel or Numbers, only if FAIRPRICE_OPEN_DRAFT=1."""
    if os.environ.get("FAIRPRICE_OPEN_DRAFT") != "1":
        return
    if sys.platform == "win32":
        os.startfile(where)
    else:  # a file path, or a download link when reports are kept in S3-compatible storage
        subprocess.run(["open" if sys.platform == "darwin" else "xdg-open", where], capture_output=True)


async def gate(run_id, tools, text, usage, ask_lines=None):
    """The human approval gate, for single items and packages: write_price_review drafts the report from what the
    run recorded, a person reads it and decides; approve carries their approval to write_price_review, and reject
    or revise deletes the draft. Returns the decision."""
    add_step(run_id, "*", "cost", usage.summary())  # the report states what the run cost
    drafted = as_json(tool_text(await tools["write_price_review"].ainvoke({"run_id": run_id})))
    print(f"\n{text}\n\n{usage.line()}\n")
    if "error" in drafted:
        print(f"No draft: {drafted['error']}\n")
    else:
        print(f"Draft report ({drafted['draft']}):\n" + "\n".join(f"  {w}" for w in drafted["files"]) + "\n")
        open_draft(drafted["files"][0])
    decision = ask_decision(can_approve="error" not in drafted)
    if decision["action"] == "revise" and ask_lines:
        decision["lines"] = ask_lines()
    request = drafted.get("request")
    if decision["action"] == "approve":  # the person at this menu is the approval write_price_review needs
        decide(request, True, decision["name"])
        decision["approval"] = request
    else:
        if request:
            decide(request, False)
        discard_review(run_id)
    audit(run_id, event="human_decision", **decision)
    return decision


def choose(prompt, options):
    """Pick one of [(value, hint)] with the arrow keys and Enter, or its number.
    Falls back to typed input when stdin is not a terminal (piped input, Windows)."""
    n, i = len(options), 0
    if not sys.stdin.isatty() or sys.platform == "win32":
        names = [v for v, _ in options]
        while True:
            answer = input(f"{prompt} [{' / '.join(names)}]: ").strip().lower()
            if answer in names:
                return answer
            if answer.isdigit() and 1 <= int(answer) <= n:
                return names[int(answer) - 1]

    import termios
    import tty

    def draw(again=True):
        if again:
            sys.stdout.write(f"\x1b[{n}A")
        for j, (value, hint) in enumerate(options):
            mark = "\x1b[1m❯ " if j == i else "  "
            sys.stdout.write(f"\r\x1b[2K{mark}{j + 1}. {value}\x1b[0m  \x1b[2m{hint}\x1b[0m\n")
        sys.stdout.flush()

    fd = sys.stdin.fileno()
    saved = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)  # before drawing: switching modes discards keys typed in between; Ctrl-C still interrupts
        print(f"{prompt} (↑/↓ and Enter, or press a number)")
        draw(again=False)
        while (key := os.read(fd, 8)) not in (b"\r", b"\n"):  # raw bytes: an arrow key is one 3-byte read
            if key in (b"\x1b[A", b"\x1b[B"):
                i = (i - 1) % n if key == b"\x1b[A" else (i + 1) % n
            elif key[:1].isdigit() and 1 <= int(key[:1]) <= n:
                i = int(key[:1]) - 1
                draw()
                break
            else:
                continue
            draw()
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)
    return options[i][0]


def approve_request(request_id):
    """A person decides a request an AI app made through MCP (write_price_review or add_country without approval)."""
    from fairprice import server
    pending = {r.id: r for r in approvals("pending")}
    if not request_id:
        print("\n".join(f"{r.id}  {r.action:<20} {r.target}  (asked {r.created_at:%d %b %H:%M})" for r in pending.values())
              or "No requests waiting for approval.")
        return
    if request_id not in pending:
        raise SystemExit(f"No pending request {request_id}. Run `fairprice --approve` to list them.")
    row = pending[request_id]
    target = row.target.split("@")[0]  # a review request is for one version of the review: <run id>@<step>
    if row.action == "write_price_review":
        with engine().connect() as db:
            draft = db.execute(select(REPORTS).where(REPORTS.c.run_id == target, REPORTS.c.status == "draft")).first()
        if draft is None:
            raise SystemExit(f"The draft for review {target} no longer exists.")
        r = draft.data["report"]
        print(f"An AI app asks to approve the price review {target}: {r['title']}, {len(draft.data['prices'])} "
              f"prices, {len(draft.data['sources'])} sources. Files: " + ", ".join(storage().location(f) for f in draft.files))
    else:
        print(f"An AI app asks to add country {target}: download its procurement data and rebuild the database.")
    action = choose("Decision", [("approve", "carry it out now, under your name"), ("reject", "refuse it")])
    name = ""
    while action == "approve" and not name:
        name = input("Your full name: ").strip()
    decide(request_id, action == "approve", name or None)
    audit(target, event="human_decision", action=action, name=name or None, request=row.action, target=row.target)
    if action == "approve":
        tool = server.write_price_review if row.action == "write_price_review" else server.add_country
        print(json.dumps(tool(target, approval=request_id), indent=2))
    else:
        if row.action == "write_price_review":
            server.discard_review(target)
        print("Rejected; nothing was changed.")


def ask_decision(can_approve=True):
    """The human gate: approve (with a name), revise (with questions) or reject."""
    options = [("approve", "stamp the report with your name and keep it")] if can_approve else []
    action = choose("Decision", options + [("revise", "send it back with questions; the draft is deleted"),
                                           ("reject", "delete the draft; nothing is kept")])
    decision = {"action": action}
    while action == "approve" and not decision.get("name"):
        decision["name"] = input("Approving officer's full name: ").strip()
    if action == "revise":
        decision["feedback"] = input("What should the agent investigate further? ").strip()
    return decision


def ollama_url():
    """Where Ollama runs: OLLAMA_HOST, the setting Ollama itself uses (e.g. http://gpu-server:11434)."""
    url = os.environ.get("OLLAMA_HOST", "http://localhost:11434").strip().rstrip("/")
    return url if "://" in url else f"http://{url}"


def model_url():
    """FAIRPRICE_MODEL_URL: your own OpenAI-compatible server, for FAIRPRICE_MODEL=openai:<name>
    (LM Studio, llama.cpp's llama-server, mlx_lm.server, Jan, LocalAI, vLLM, SGLang, TGI, NIM; e.g. http://localhost:1234/v1)."""
    return os.environ.get("FAIRPRICE_MODEL_URL", "").strip().rstrip("/")


def model_settings(model_name):
    """Arguments for init_chat_model: temperature 0, a 4,096-token output cap and a 10-minute timeout on every route."""
    kwargs = {"temperature": 0}
    if model_name.startswith("ollama:"):
        # Thinking at temperature 0 can loop for an hour on Qwen3; cap output so a run always ends.
        kwargs.update(num_ctx=16384, num_predict=4096, reasoning=False, base_url=ollama_url(),
                      client_kwargs={"timeout": 600})  # a dropped connection fails the run instead of hanging it
    elif model_name.startswith("openai:"):  # the context window is a server setting on this route (see README)
        kwargs.update(base_url=model_url(), api_key=os.environ.get("FAIRPRICE_MODEL_API_KEY") or "not-needed",
                      max_tokens=4096, timeout=600, max_retries=1)
    return kwargs


TOOL_FLAGS = ("Turn tool calling on: for vLLM start it with --enable-auto-tool-choice --tool-call-parser <the model's "
              "parser>; for llama.cpp add --jinja; in LM Studio choose a model with tool use; mlx_lm.server and Ollama "
              "need a model whose chat template supports tools.")
PING = {"type": "function", "function": {"name": "ping", "description": "Answer a ping.",
                                         "parameters": {"type": "object", "properties": {}}}}


def calls_tools(model):
    """One short request with a dummy tool: '' if the model called it, or why not."""
    try:
        reply = model.bind_tools([PING]).invoke("Call the ping tool now.")
    except Exception as e:
        return f"the test request failed ({e!r})"
    return "" if getattr(reply, "tool_calls", None) else "it answered in text instead of calling a tool"


CONTEXT_FLAGS = ("Set the server's context window to at least 16,384 tokens: Ollama `OLLAMA_CONTEXT_LENGTH=16384` "
                 "(or num_ctx in a Modelfile), llama.cpp `-c 16384`, vLLM `--max-model-len 16384`, LM Studio's "
                 "context-length setting.")


def context_problem(model, tokens=7000):
    """'' if the server reads a long prompt whole, or what it did instead. A server that silently cuts the
    conversation makes the agent lose its own search results and repeat itself."""
    try:
        reply = model.invoke("word " * tokens + "\nReply with the single word OK.")
    except Exception as e:
        return f"the long test request failed ({e!r})"
    read = (getattr(reply, "usage_metadata", None) or {}).get("input_tokens", 0)
    return "" if read >= tokens * 0.9 else f"it read only {read:,} of about {tokens:,} tokens"


def check_server(name):
    """An OpenAI-compatible server: set, installed, reachable, serving the model, and calling tools."""
    url = model_url()
    if not url:
        return ["FAIRPRICE_MODEL=openai:… needs FAIRPRICE_MODEL_URL, the address of your own OpenAI-compatible server, "
                "e.g. http://localhost:1234/v1 for LM Studio. FairPrice does not send procurement data to hosted APIs."]
    if importlib.util.find_spec("langchain_openai") is None:
        return ["Run `uv sync --extra openai-compatible` to use an OpenAI-compatible server."]
    key = os.environ.get("FAIRPRICE_MODEL_API_KEY")
    req = urllib.request.Request(f"{url}/models", headers={"Authorization": f"Bearer {key}"} if key else {})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            have = {m["id"] for m in json.load(r).get("data", [])}
    except (OSError, ValueError) as e:
        return [f"No OpenAI-compatible server at {url} ({e}). Start it (LM Studio, llama-server, mlx_lm.server, "
                "vLLM, ...) or correct FAIRPRICE_MODEL_URL."]
    if name not in have:
        return [f"The server at {url} has no model {name!r}. It has: {', '.join(sorted(have)) or 'none'}. "
                "Set FAIRPRICE_MODEL=openai:<one of those>."]
    if why := calls_tools(init_chat_model(f"openai:{name}", **model_settings(f"openai:{name}"))):
        return [f"The model {name!r} at {url} cannot be used: {why}. FairPrice's agent works by calling tools. {TOOL_FLAGS}"]
    return []


def check_model(model_name=MODEL):
    """What would stop a run, each with how to fix it; empty when the model is ready."""
    if model_name.startswith("openai:"):
        return check_server(model_name.removeprefix("openai:"))
    if not model_name.startswith("ollama:"):
        return []  # other providers report their own errors
    name, url = model_name.removeprefix("ollama:"), ollama_url()
    try:
        with urllib.request.urlopen(f"{url}/api/tags", timeout=5) as r:
            have = {m["name"] for m in json.load(r).get("models", [])}
    except (OSError, ValueError) as e:
        return [f"Ollama is not reachable at {url} ({e}). Install it from https://ollama.com/download and open it, "
                "or set OLLAMA_HOST to where it runs, e.g. OLLAMA_HOST=http://gpu-server:11434."]
    if name not in have and f"{name}:latest" not in have:
        where = "" if any(h in url for h in ("localhost", "127.0.0.1")) else " on the machine running Ollama"
        return [f"Ollama at {url} does not have the model {name!r}. Run `ollama pull {name}`{where}."]
    return []


def check_setup():
    """`fairprice --check`: everything a run needs, with how to fix what is missing."""
    failures = 0

    def show(good, text, fix=""):
        nonlocal failures
        failures += good is False
        print(f"{'✓' if good else '✗' if good is False else '•'} {text}" + (f"\n    {fix}" if fix else ""))

    problems = check_model(MODEL)
    if not problems and MODEL.startswith("openai:"):  # only here: the long test request costs some seconds
        short = context_problem(init_chat_model(MODEL, **model_settings(MODEL)))
        problems = [f"The server cuts long conversations: {short}. {CONTEXT_FLAGS}"] if short else []
    where = (ollama_url() if MODEL.startswith("ollama:") else (model_url() or "(FAIRPRICE_MODEL_URL not set)")
             if MODEL.startswith("openai:") else "")
    show(not problems, f"Model {MODEL}" + (f" at {where}" if where else ""), problems[0] if problems else "")
    if Path(DB).exists():
        with duckdb.connect(DB, read_only=True) as con:
            records, countries = con.execute("SELECT count(*), string_agg(DISTINCT country, ', ' ORDER BY country) FROM lines").fetchone()
        show(True, f"Procurement data: {records:,} records ({countries})")
    else:
        show(None, "Procurement data: not built yet; the first run downloads about 60 MB, or run `uv run fairprice-ingest`")
    cached = borrowed_args()[0] == "--offline"
    show(True if cached else None, "Borrowed DuckDB MCP server: " + ("cached" if cached else "downloads on the first run (needs internet once)"))
    try:
        store = storage()
        show(True, f"Reports: {type(store).__name__.replace('Storage', '').lower()} storage, "
                   f"{os.environ.get('FAIRPRICE_REPORT_FORMAT', 'xlsx')} files")
    except Exception as e:
        show(False, f"Reports: {e!r}", "For FAIRPRICE_STORAGE=s3, run `uv sync --extra s3` and set FAIRPRICE_S3_BUCKET.")
    try:
        with engine().connect():
            pass
        show(True, "Database for runs, reports and the audit log: reachable")
    except Exception as e:
        show(False, f"Database for runs, reports and the audit log: {e!r}", "Check FAIRPRICE_SQL_URL.")
    print("Ready." if not failures else f"{failures} problem(s) to fix before a run.")
    return failures


class Usage(BaseCallbackHandler):
    """What a run cost: model calls, tokens, model time and, if prices are set, API fees."""

    def __init__(self):
        self.calls = self.input_tokens = self.output_tokens = 0
        self.seconds, self.started = 0.0, {}

    def on_chat_model_start(self, serialized, messages, *, run_id, **kwargs):
        self.started[run_id] = time.perf_counter()

    def on_llm_end(self, response, *, run_id, **kwargs):
        self.calls += 1
        self.seconds += time.perf_counter() - self.started.pop(run_id, time.perf_counter())
        for generations in response.generations:
            for g in generations:
                usage = getattr(getattr(g, "message", None), "usage_metadata", None) or {}
                self.input_tokens += usage.get("input_tokens", 0)
                self.output_tokens += usage.get("output_tokens", 0)

    def summary(self):
        price_in = float(os.environ.get("FAIRPRICE_PRICE_PER_MILLION_INPUT_TOKENS", 0))
        price_out = float(os.environ.get("FAIRPRICE_PRICE_PER_MILLION_OUTPUT_TOKENS", 0))
        return {"model": MODEL, "model_calls": self.calls, "input_tokens": self.input_tokens,
                "output_tokens": self.output_tokens, "model_seconds": round(self.seconds, 1),
                "api_fees": round((self.input_tokens * price_in + self.output_tokens * price_out) / 1e6, 4),
                "api_fees_currency": os.environ.get("FAIRPRICE_PRICE_CURRENCY", "USD")}

    def line(self):
        c = self.summary()
        fees = f"{c['api_fees_currency']} {c['api_fees']:.2f}" + (" (local model)" if c["api_fees"] == 0 else "")
        return (f"Cost of this run: {c['model_calls']} model call{'s' if c['model_calls'] != 1 else ''}, {c['input_tokens']:,} input and "
                f"{c['output_tokens']:,} output tokens, {c['model_seconds']:.0f} s of model time, API fees {fees}.")


@contextlib.asynccontextmanager
async def session(model_name=MODEL):
    """Connect both MCP servers and the model; yield the compiled graph and our own tools by name."""
    if not Path(DB).exists():
        print("First run: building the local database (about a minute)...")
        ingest.main([])
    client = MultiServerMCPClient(servers(), tool_interceptors=[audit_interceptor])
    own_tools = await client.get_tools(server_name="fairprice")
    borrowed_tools = await client.get_tools(server_name="duckdb")
    if problems := check_model(model_name):
        raise SystemExit("\n".join(problems))
    model = init_chat_model(model_name, **model_settings(model_name))
    (ROOT / "data").mkdir(exist_ok=True)  # the database may be configured elsewhere; run state always lives here
    async with AsyncSqliteSaver.from_conn_string(str(ROOT / "data" / "runs.sqlite")) as saver:
        yield build_graph(own_tools, borrowed_tools, model, saver), {t.name: t for t in own_tools}


async def run(args):
    global CURRENT_RUN
    usage = Usage()
    async with session() as (graph, tools):
        if args.resume:
            run_id, payload = args.resume, None
        else:
            started = as_json(tool_text(await tools["start_review"].ainvoke(
                {"item": args.item, "unit_price": args.unit_price, "currency": args.currency or "", "year": args.year,
                 "country": args.country})))
            if "error" in started:
                raise SystemExit(started["error"])
            run_id = started["run_id"]
            req = {"run_id": run_id, "line": "1", "item": args.item, "unit_price": args.unit_price,
                   "currency": started["currency"], "year": started["year"], "country": started["country"]}
            payload = {"request": req}
            audit(run_id, event="run_started", request=req, model=MODEL)
            print(f"Run {run_id} | model {MODEL}")
        CURRENT_RUN = run_id
        config = {"configurable": {"thread_id": run_id}, "callbacks": [usage]}
        while True:
            async for update in graph.astream(payload, config, stream_mode="updates"):
                for node in update:
                    if node not in ("__interrupt__", "approve"):
                        print(f"[{node}] done", flush=True)
            snapshot = await graph.aget_state(config)
            if not snapshot.next:
                break
            payload = Command(resume=await gate(run_id, tools, snapshot.values["report"], usage))
        final = (await graph.aget_state(config)).values
        print(final.get("saved") or "Not saved: the reviewer rejected the draft.")
        audit(run_id, event="run_finished", saved=final.get("saved"), cost=usage.summary())


def main():
    p = argparse.ArgumentParser(description="Prepare a sourced price-reasonableness file.")
    p.add_argument("item", nargs="?", help="what is being bought, e.g. 'Toyota Hilux double cabin'")
    p.add_argument("--unit-price", type=float)
    p.add_argument("--currency", help="currency of the quoted prices, e.g. NGN, KES, RWF (default: the country's)")
    p.add_argument("--year", type=int, default=dt.date.today().year)
    p.add_argument("--country", default="NG", help="ISO code of a loaded country (see fairprice-ingest --list)")
    p.add_argument("--package", "--sheet", dest="sheet", metavar="FILE",
                   help="a bid package (.xlsx, .csv or .json) to review line by line; see README")
    p.add_argument("--resume", help="resume a paused run by its id (with --package: reuse that batch's finished lines)")
    p.add_argument("--audit", metavar="RUN_ID", help="print the audit log of a run or batch, then exit")
    p.add_argument("--check", action="store_true", help="check the setup (model, data, storage) and say how to fix it")
    p.add_argument("--approve", metavar="REQUEST", nargs="?", const="",
                   help="decide a request from an AI app to approve a report or add a country (no id: list them)")
    p.add_argument("--format", choices=report.FORMATS, default=os.environ.get("FAIRPRICE_REPORT_FORMAT", "xlsx"),
                   help="report file format (default: FAIRPRICE_REPORT_FORMAT, or xlsx)")
    args = p.parse_args()
    os.environ["FAIRPRICE_REPORT_FORMAT"] = args.format
    if args.check:
        raise SystemExit(1 if check_setup() else 0)
    if args.audit:
        return show_audit(args.audit)
    if args.approve is not None:
        return approve_request(args.approve)
    if args.sheet:
        from fairprice import batch
        return asyncio.run(batch.run(args))
    if not args.resume and not (args.item and args.unit_price):
        p.error("give an item and --unit-price, a --package, or --resume <run id>")
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
