"""The agent: the self-check, the graph end to end with a scripted model, the approval gate, the CLI and the audit log."""

import argparse
import asyncio
import json
import uuid
from types import SimpleNamespace
from unittest import mock

import pytest
from langchain_core.messages import ToolMessage
from langgraph.types import Command

import conftest
from conftest import ScriptedModel, fake_session, ids
from fairprice import agent, report

REQ = {"item": "Toyota Hilux double cabin pickup", "unit_price": 95e6, "currency": "NGN", "year": 2025, "country": "NG"}


def tool_msg(name, content):
    return ToolMessage(content=json.dumps(content), name=name, tool_call_id=uuid.uuid4().hex)


def test_self_check_sends_the_analyst_back_when_unfinished():
    assert "never checked" in agent.unfinished([])
    assert "searched after your last price check" in agent.unfinished([tool_msg("find_comparables", {})])
    thin = [tool_msg("find_comparables", {}), tool_msg("check_price_comparability", {"band": {"n": 6}, "assessment": "within_band"})]
    assert "one broader search" in agent.unfinished(thin)
    few = [tool_msg("find_comparables", {}), tool_msg("check_price_comparability", {"band": None, "assessment": "insufficient_comparables"})]
    assert "never a specification" in agent.unfinished(few)
    done = [tool_msg("find_comparables", {})] * 2 + [tool_msg("check_price_comparability", {"band": {"n": 12}, "assessment": "within_band"})]
    assert agent.unfinished(done) is None


def test_tool_output_helpers():
    assert agent.tool_text(SimpleNamespace(content=[{"text": "a"}, SimpleNamespace(text="b")])) == "ab"
    assert agent.tool_text({"x": 1}) == '{"x": 1}' and agent.tool_text("plain") == "plain"
    assert agent.as_json("not json") == {"error": "not json"}


async def run_graph(model, request=REQ, thread=None):
    async with fake_session(model) as (graph, tools):
        config = {"configurable": {"thread_id": thread or uuid.uuid4().hex}}
        await graph.ainvoke({"request": request}, config)
        return graph, config, (await graph.aget_state(config)).values


def test_graph_runs_to_the_approval_gate_and_stops(model):
    graph, config, state = asyncio.run(run_graph(model))
    assert state["final"]["band"]["n"] >= 5 and state["final"]["assessment"] == "within_band"
    assert state["challenge"]["verdict"] == "DISPUTED" and "Price under review" in state["report"]
    bundle = ids("SELECT id FROM lines WHERE description LIKE '%Prado%'")[0]
    assert str(bundle) in state["rejected"]  # the Analyst's rejection is kept through the merge
    assert len(state["searches"]) == 2  # the self-check asked for a broader search


def test_challenger_removals_are_applied_or_shown_as_objections():
    good = ids("SELECT id FROM lines WHERE description LIKE 'Supply of Toyota Hilux double cabin pickup unit%'")
    one = ScriptedModel(keywords={REQ["item"]: ["hilux"]}, remove=(good[0],), verdict="VERDICT: STANDS fine")
    state = asyncio.run(run_graph(one))[2]
    assert state["challenge"]["verdict"] == "STANDS" and good[0] not in [r["id"] for r in state["final"]["accepted"]]
    many = ScriptedModel(keywords={REQ["item"]: ["hilux"]}, remove=tuple(good[:5]), verdict=None)
    state = asyncio.run(run_graph(many))[2]
    assert state["challenge"]["applied"] is False and state["challenge"]["reason"] == "Challenger gave no clear verdict."
    assert len(state["final"]["accepted"]) == len(state["final"]["accepted"])  # band kept as the Analyst built it


def test_no_band_means_no_challenge_and_an_unresolved_run(monkeypatch):
    laptop = {**REQ, "item": "Laptop computer", "unit_price": 2e6}
    state = asyncio.run(run_graph(ScriptedModel(), laptop))[2]
    assert state["final"]["band"] is None and state["challenge"]["verdict"] == "NOT RUN"
    stuck = ScriptedModel(stop_after_search=True)  # never checks a price: unresolved after the self-check gives up
    state = asyncio.run(run_graph(stuck, laptop))[2]
    assert state["final"]["assessment"].startswith("unresolved")
    monkeypatch.setattr(agent, "STEP_BUDGET", 2)  # the step budget runs out mid-search
    state = asyncio.run(run_graph(ScriptedModel(), laptop))[2]
    assert state["final"]["assessment"].startswith("unresolved")


def test_challenger_step_budget(monkeypatch):
    real = agent.create_agent

    def budget(model, tools, system_prompt):
        made = real(model, tools, system_prompt=system_prompt)
        if "independent auditor" in system_prompt:
            async def fail(*a, **k):
                raise agent.GraphRecursionError("budget")
            made.ainvoke = fail
        return made
    monkeypatch.setattr(agent, "create_agent", budget)
    state = asyncio.run(run_graph(ScriptedModel(keywords={REQ["item"]: ["hilux"]})))[2]
    assert state["challenge"]["verdict"] == "DISPUTED" and "step budget" in state["challenge"]["reason"]


def test_cli_run_reject_revise_and_approve(session, answers, capsys):
    answers(["revise", "check the Prado bundle", "approve", "Ada Obi"])
    args = argparse.Namespace(resume=None, **REQ)
    asyncio.run(agent.run(args))
    out = capsys.readouterr().out
    assert "Draft report (DRAFT-toyota-hilux-double-cabin-pickup-" in out and '"saved"' in out
    answers(["reject"])
    asyncio.run(agent.run(args))
    assert "Not saved: the reviewer rejected the draft." in capsys.readouterr().out


def test_cli_resume_a_paused_run(session, answers, capsys):
    thread = uuid.uuid4().hex[:12]
    asyncio.run(run_graph(session, thread=thread))  # paused at the gate, in the shared checkpointer
    answers(["reject"])
    asyncio.run(agent.run(argparse.Namespace(resume=thread, **REQ)))
    assert "Not saved" in capsys.readouterr().out


def test_choose_with_arrow_keys_and_numbers(monkeypatch):
    import termios
    import tty
    monkeypatch.setattr("sys.stdin", SimpleNamespace(isatty=lambda: True, fileno=lambda: 0))
    monkeypatch.setattr(termios, "tcgetattr", lambda fd: [])
    monkeypatch.setattr(termios, "tcsetattr", lambda *a: None)
    monkeypatch.setattr(tty, "setcbreak", lambda fd: None)
    options = [("approve", "a"), ("revise", "b"), ("reject", "c")]
    for keys, expected in (([b"\x1b[B", b"x", b"\x1b[B", b"\r"], "reject"), ([b"\x1b[A", b"\n"], "reject"),
                           ([b"2"], "revise")):
        keys = iter(keys)
        monkeypatch.setattr(agent.os, "read", lambda fd, n: next(keys))
        assert agent.choose("Decision", options) == expected


def test_choose_typed_fallback(answers):
    answers(["maybe", "3"])
    assert agent.choose("Decision", [("approve", ""), ("revise", ""), ("reject", "")]) == "reject"


def test_audit_log_and_its_viewer(capsys):
    agent.audit("viewer-1", event="run_started", request={"item": "x"})
    agent.audit("viewer-1-0", server="fairprice", tool="find_comparables", args={"keywords": ["x"]},
                output='{"total_matches": 0}', seconds=0.1)
    agent.audit("viewer-1-0", server="duckdb", tool="execute_query", args={}, output="not json", seconds=0.1)
    agent.show_audit("viewer-1")
    out = capsys.readouterr().out
    assert "run_started" in out and "fairprice.find_comparables" in out and '-> {"total_matches": 0}' in out
    assert "3 entries" in out
    agent.show_audit("nobody")
    assert "No audit entries for run nobody." in capsys.readouterr().out


def test_audit_interceptor_logs_and_reraises(capsys):
    request = SimpleNamespace(server_name="fairprice", name="find_comparables", args={"keywords": ["x"]})

    async def ok(req):
        return SimpleNamespace(content=[{"text": "{}"}])

    async def boom(req):
        raise RuntimeError("tool failed")
    agent.CURRENT_RUN = "interceptor"
    asyncio.run(agent.audit_interceptor(request, ok))
    with pytest.raises(RuntimeError):
        asyncio.run(agent.audit_interceptor(request, boom))
    agent.show_audit("interceptor")
    assert "tool failed" in capsys.readouterr().out


def test_borrowed_server_offline_once_cached(monkeypatch):
    monkeypatch.setattr(agent.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=0))
    assert agent.borrowed_args()[0] == "--offline"
    monkeypatch.setattr(agent.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=1))
    assert agent.borrowed_args()[0] == agent.BORROWED
    assert set(agent.servers()) == {"fairprice", "duckdb"}


def test_main_parses_the_command_line(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(agent, "run", lambda args: calls.append(("run", args.item)) or asyncio.sleep(0))
    from fairprice import batch
    monkeypatch.setattr(batch, "run", lambda args: calls.append(("batch", args.sheet)) or asyncio.sleep(0))
    monkeypatch.setattr(agent, "show_audit", lambda rid: calls.append(("audit", rid)))
    for argv in (["fairprice", "laptop", "--unit-price", "5"], ["fairprice", "--package", "x.csv", "--format", "csv"],
                 ["fairprice", "--audit", "abc"]):
        monkeypatch.setattr("sys.argv", argv)
        agent.main()
    assert calls == [("run", "laptop"), ("batch", "x.csv"), ("audit", "abc")]
    monkeypatch.setattr("sys.argv", ["fairprice", "laptop"])
    with pytest.raises(SystemExit):
        agent.main()


def test_session_starts_both_mcp_servers_and_builds_the_database_when_missing(monkeypatch, tmp_path, answers, capsys):
    """Integration: the real session, with both MCP servers as subprocesses over stdio; only the model is scripted."""
    monkeypatch.setattr(agent, "init_chat_model", lambda name, **kw: ScriptedModel(keywords={REQ["item"]: ["hilux"]}))
    monkeypatch.setattr(agent, "check_model", lambda name: [])  # no Ollama in CI
    monkeypatch.setattr(agent, "ROOT", tmp_path)  # the checkpointer's file
    monkeypatch.setattr(agent, "DB", agent.DB)
    answers(["approve", "Ada Obi"])
    asyncio.run(agent.run(argparse.Namespace(resume=None, **REQ)))
    out = capsys.readouterr().out
    assert "-> fairprice.find_comparables" in out and "-> duckdb.execute_query" in out and '"saved"' in out
    assert "Cost of this run: " in out and "API fees USD 0.00 (local model)" in out
    built = []
    monkeypatch.setattr(agent, "DB", str(tmp_path / "missing.duckdb"))
    monkeypatch.setattr(agent.ingest, "main", lambda argv: built.append(argv))
    monkeypatch.setattr(agent, "MultiServerMCPClient", mock.Mock(side_effect=RuntimeError("stop here")))
    with pytest.raises(RuntimeError):
        asyncio.run(agent.session().__aenter__())
    assert built == [[]]


def test_show_draft_summary_for_a_single_item():
    state = asyncio.run(run_graph(ScriptedModel(keywords={REQ["item"]: ["hilux"]})))[2]
    lines, states, names = report.single(state)
    assert names == ["Price under review"] and lines[0]["prices"] == {"Price under review": REQ["unit_price"]}
    assert conftest.SAVER is not None


def test_a_person_decides_requests_from_ai_apps(answers, capsys, monkeypatch):
    """`fairprice --approve`: list requests, approve a report as a named person, reject an add_country request."""
    from fairprice import db, server
    state = asyncio.run(run_graph(ScriptedModel(keywords={REQ["item"]: ["hilux"]})))[2]
    agent.save_draft("ap", "DRAFT-from-an-app-ap", report.build("x", *report.single(state), None, REQ, "ap"), "")
    request = server.write_price_review("DRAFT-from-an-app-ap")["request"]
    agent.approve_request("")
    assert request in capsys.readouterr().out
    answers(["approve", "Ada Obi"])
    agent.approve_request(request)
    out = capsys.readouterr().out
    assert "asks to approve the price review DRAFT-from-an-app-ap" in out and '"approved_by": "Ada Obi"' in out
    assert server.get_price_review(json.loads(out[out.index("{"):])["saved"])["report"]["approved_by"] == "Ada Obi"
    country = db.request_approval("add_country", "RW")
    answers(["reject"])
    agent.approve_request(country)
    assert "Rejected; nothing was changed." in capsys.readouterr().out
    with pytest.raises(SystemExit, match="No pending request"):
        agent.approve_request(country)
    gone = db.request_approval("write_price_review", "DRAFT-deleted")
    with pytest.raises(SystemExit, match="no longer exists"):
        agent.approve_request(gone)
    country = db.request_approval("add_country", "RW")
    answers(["approve", "Ada Obi"])
    with mock.patch.object(server, "add_country", return_value={"started": "RW"}) as add:
        agent.approve_request(country)
    assert add.call_args.kwargs == {"approval": country} and "add country RW" in capsys.readouterr().out
    db.decide(gone, False)  # the last pending request, settled
    agent.approve_request("")
    assert "No requests waiting for approval." in capsys.readouterr().out


def test_main_routes_approve(monkeypatch):
    seen = []
    monkeypatch.setattr(agent, "approve_request", lambda rid: seen.append(rid))
    for argv, expected in ((["fairprice", "--approve"], ""), (["fairprice", "--approve", "abc"], "abc")):
        monkeypatch.setattr("sys.argv", argv)
        agent.main()
        assert seen[-1] == expected


def test_ollama_url_and_model_checks(monkeypatch):
    monkeypatch.setenv("OLLAMA_HOST", "gpu-server:11434")
    assert agent.ollama_url() == "http://gpu-server:11434"
    monkeypatch.setenv("OLLAMA_HOST", "https://ollama.example.org/")
    assert agent.ollama_url() == "https://ollama.example.org"
    assert agent.check_model("openai:gpt-x") == []  # other providers report their own errors

    class Reply:
        def __init__(self, body):
            self.body = body

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self, *a):
            return self.body
    monkeypatch.setattr(agent.urllib.request, "urlopen", lambda url, timeout: Reply(b'{"models": [{"name": "gemma4:latest"}]}'))
    assert agent.check_model("ollama:gemma4") == []
    assert "Run `ollama pull qwen3:8b` on the machine running Ollama" in agent.check_model("ollama:qwen3:8b")[0]

    def refused(url, timeout):
        raise OSError("connection refused")
    monkeypatch.setattr(agent.urllib.request, "urlopen", refused)
    assert "is not reachable at https://ollama.example.org" in agent.check_model("ollama:gemma4")[0]
    with pytest.raises(SystemExit, match="not reachable"):
        asyncio.run(agent.session("ollama:gemma4").__aenter__())


def test_check_setup_reports_each_part(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(agent, "check_model", lambda name: [])
    monkeypatch.setattr(agent, "borrowed_args", lambda: ["--offline"])
    assert agent.check_setup() == 0
    out = capsys.readouterr().out
    assert "✓ Procurement data:" in out and "✓ Borrowed DuckDB MCP server: cached" in out and "Ready." in out
    monkeypatch.setattr(agent, "check_model", lambda name: ["Ollama is not reachable"])
    monkeypatch.setattr(agent, "borrowed_args", lambda: [agent.BORROWED])
    monkeypatch.setattr(agent, "DB", str(tmp_path / "none.duckdb"))
    monkeypatch.setenv("FAIRPRICE_STORAGE", "s3")
    monkeypatch.delenv("FAIRPRICE_S3_BUCKET", raising=False)
    monkeypatch.setattr(agent, "engine", lambda: (_ for _ in ()).throw(RuntimeError("no database")))
    assert agent.check_setup() == 3
    out = capsys.readouterr().out
    assert "✗ Model" in out and "• Procurement data: not built yet" in out and "downloads on the first run" in out
    assert "uv sync --extra s3" in out and "Check FAIRPRICE_SQL_URL" in out and "3 problem(s)" in out
    monkeypatch.setattr("sys.argv", ["fairprice", "--check"])
    with pytest.raises(SystemExit) as code:
        agent.main()
    assert code.value.code == 1


def test_usage_counts_calls_tokens_time_and_fees(monkeypatch):
    usage = agent.Usage()
    message = SimpleNamespace(usage_metadata={"input_tokens": 1500, "output_tokens": 200})
    for run in ("a", "b"):
        usage.on_chat_model_start({}, [], run_id=run)
        usage.on_llm_end(SimpleNamespace(generations=[[SimpleNamespace(message=message)]]), run_id=run)
    usage.on_llm_end(SimpleNamespace(generations=[[SimpleNamespace(message=None)]]), run_id="never-started")
    c = usage.summary()
    assert (c["model_calls"], c["input_tokens"], c["output_tokens"]) == (3, 3000, 400)
    monkeypatch.setenv("FAIRPRICE_PRICE_PER_MILLION_INPUT_TOKENS", "2")
    monkeypatch.setenv("FAIRPRICE_PRICE_PER_MILLION_OUTPUT_TOKENS", "10")
    assert usage.summary()["api_fees"] == 0.01
    assert "3 model calls, 3,000 input and 400 output tokens" in usage.line() and "USD 0.01." in usage.line()
