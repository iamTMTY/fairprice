"""The agent: the self-check, the graph end to end with a scripted model, the approval gate, the CLI and the audit log."""

import argparse
import asyncio
import json
import uuid
from types import SimpleNamespace
from unittest import mock

import pytest
from langchain_core.messages import AIMessage, ToolMessage

import conftest
from conftest import ScriptedModel, fake_session, ids
from fairprice import agent, report, server

REQ = {"item": "Toyota Hilux double cabin pickup", "unit_price": 95e6, "currency": "NGN", "year": 2025, "country": "NG"}


def tool_msg(name, content):
    return ToolMessage(content=json.dumps(content), name=name, tool_call_id=uuid.uuid4().hex)


def plan(*searches):
    """The Analyst's plan_searches call and its answer."""
    call = AIMessage("", tool_calls=[{"name": "plan_searches", "args": {"searches": list(searches)}, "id": uuid.uuid4().hex}])
    return [call, tool_msg("plan_searches", {"recorded": True})]


def searched(keywords, matches):
    call = AIMessage("", tool_calls=[{"name": "find_comparables", "args": {"keywords": keywords}, "id": uuid.uuid4().hex}])
    return [call, tool_msg("find_comparables", {"total_matches": matches})]


def test_self_check_sends_the_analyst_back_when_unfinished():
    assert "plan_searches" in agent.unfinished([])  # planning comes first
    assert "never checked" in agent.unfinished(plan(["hilux"]))
    assert "searched after your last price check" in agent.unfinished(plan(["hilux"]) + searched(["hilux"], 3))
    thin = searched(["hilux"], 6) + [tool_msg("check_price_comparability", {"band": {"n": 6}, "assessment": "within_band"})]
    assert "one broader search" in agent.unfinished(plan(["hilux"]) + thin)
    assert "next search in your plan (['pickup'])" in agent.unfinished(plan(["hilux"], ["pickup"]) + thin)
    few = searched(["hilux"], 2) + [tool_msg("check_price_comparability", {"band": None, "assessment": "insufficient_comparables"})]
    assert "never a specification" in agent.unfinished(plan(["hilux"]) + few)
    assert "Run the next search in your plan: ['pickup']" in agent.unfinished(plan(["hilux"], ["pickup"]) + few)
    nothing = plan(["hilux double cabin"], ["cabin", "double hilux"], ["pickup"]) + searched(["double cabin hilux"], 0)
    assert "Every search so far found nothing. Run the next search in your plan: ['pickup']" in agent.unfinished(nothing)
    assert "single word" in agent.unfinished(plan(["hilux"]) + searched(["hilux"], 0))
    done = searched(["hilux"], 12) * 2 + [tool_msg("check_price_comparability", {"band": {"n": 12}, "assessment": "within_band"})]
    assert agent.unfinished(plan(["hilux"]) + done) is None
    assert agent.planned([]) == [] and agent.next_planned(plan(["a b"]) + searched(["b", "a"], 1)) is None


def test_bound_tools_hide_the_run_from_the_model():
    async def go():
        async with fake_session(ScriptedModel()) as (_, tools):
            run = server.start_review("Laptop computer", 2e6, year=2025)["run_id"]
            search = agent.bound(tools["find_comparables"], run, "1")
            assert "run_id" not in search.args and "line" not in search.args and "keywords" in search.args
            return json.loads(await search.ainvoke({"keywords": ["laptop"]})), run
    found, run = asyncio.run(go())
    assert found["total_matches"] >= 2 and server.review(run)[0].id == run


def test_tool_output_helpers():
    assert agent.tool_text(SimpleNamespace(content=[{"text": "a"}, SimpleNamespace(text="b")])) == "ab"
    assert agent.tool_text({"x": 1}) == '{"x": 1}' and agent.tool_text("plain") == "plain"
    assert agent.as_json("not json") == {"error": "not json"}


def started(request=REQ):
    """A review started the way the CLI starts one: the request carries its run_id and line."""
    run = server.start_review(request["item"], request["unit_price"], request["currency"], request["year"],
                              request["country"])["run_id"]
    return {**request, "run_id": run, "line": "1"}


async def run_graph(model, request=REQ):
    request = started(request)
    async with fake_session(model) as (graph, tools):
        config = {"configurable": {"thread_id": request["run_id"]}}
        await graph.ainvoke({"request": request}, config)
        return graph, config, (await graph.aget_state(config)).values


def test_graph_runs_to_the_approval_gate_and_stops(model):
    graph, config, state = asyncio.run(run_graph(model))
    assert state["final"]["band"]["n"] >= 5 and state["final"]["assessment"] == "within_band"
    assert state["challenge"]["verdict"] == "DISPUTED" and "Price under review" in state["report"]
    bundle = ids("SELECT id FROM lines WHERE description LIKE '%Prado%'")[0]
    assert str(bundle) in state["rejected"]  # the Analyst's rejection is kept through the merge
    assert len(state["searches"]) == 2  # the self-check asked for the next planned search
    recorded = server.review(state["request"]["run_id"])[2]["1"]
    assert recorded["final"] == state["final"] and recorded["plan"]["searches"] == [["hilux"], ["pickup"]]
    assert recorded["challenge"]["verdict"] == "DISPUTED"  # the Challenger's verdict is a recorded step too


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
    assert report.NOTICE in out and '"approved_by": "Ada Obi"' in out and "toyota-hilux-double-cabin-pickup-" in out
    answers(["reject"])
    asyncio.run(agent.run(args))
    assert "Not saved: the reviewer rejected it." in capsys.readouterr().out


def test_cli_resume_a_paused_run(session, answers, capsys):
    _, config, _ = asyncio.run(run_graph(session))  # paused at the gate, in the shared checkpointer
    answers(["reject"])
    asyncio.run(agent.run(argparse.Namespace(resume=config["configurable"]["thread_id"], **REQ)))
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
    """The borrowed server's calls are logged by the client; our own server logs its calls itself."""
    ours = SimpleNamespace(server_name="fairprice", name="find_comparables", args={"keywords": ["x"]})
    request = SimpleNamespace(server_name="duckdb", name="execute_query", args={"sql": "SELECT 1"})

    async def ok(req):
        return SimpleNamespace(content=[{"text": "{}"}])

    async def boom(req):
        raise RuntimeError("tool failed")
    agent.CURRENT_RUN = "interceptor"
    asyncio.run(agent.audit_interceptor(request, ok))
    with pytest.raises(RuntimeError):
        asyncio.run(agent.audit_interceptor(request, boom))
    asyncio.run(agent.audit_interceptor(ours, ok))
    agent.show_audit("interceptor")
    out = capsys.readouterr().out
    assert "tool failed" in out and "2 entries" in out and "fairprice.find_comparables {" not in out


def test_borrowed_server_offline_once_cached(monkeypatch):
    monkeypatch.setattr(agent.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=0))
    assert agent.borrowed_args()[0] == "--offline"
    monkeypatch.setattr(agent.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=1))
    assert agent.borrowed_args()[0] == agent.BORROWED
    found = agent.servers()
    assert set(found) == {"fairprice", "duckdb"}
    assert found["duckdb"]["args"][2:4] == [str(agent.BORROWED_LOG), "uvx"]  # its log lines go to a file


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
    assert "Cost of this run: " in out and "API fees $0.00 (local model)($" in out
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


def test_ollama_url_and_model_checks(monkeypatch):
    monkeypatch.setenv("OLLAMA_HOST", "gpu-server:11434")
    assert agent.ollama_url() == "http://gpu-server:11434"
    monkeypatch.setenv("OLLAMA_HOST", "https://ollama.example.org/")
    assert agent.ollama_url() == "https://ollama.example.org"
    assert agent.check_model("anthropic:x") == []  # other providers report their own errors

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
    # a local model: no fees, and what the same tokens cost on the closest hosted model (the smallest Gemma 4)
    assert (c["local_model"], c["api_fees"], c["priced_as"].split(" ")[0]) == (True, 0.0, "google/gemma-4-26b-a4b-it")
    assert usage.line().endswith("API fees $0.00 (local model)($0.00039 saved)")
    monkeypatch.setenv("FAIRPRICE_PRICE_PER_MILLION_INPUT_TOKENS", "2")  # a negotiated price wins
    monkeypatch.setenv("FAIRPRICE_PRICE_PER_MILLION_OUTPUT_TOKENS", "10")
    assert usage.line().endswith("($0.01 saved)") and usage.summary()["priced_as"] == "your price"
    monkeypatch.delenv("FAIRPRICE_PRICE_PER_MILLION_INPUT_TOKENS")
    monkeypatch.delenv("FAIRPRICE_PRICE_PER_MILLION_OUTPUT_TOKENS")
    usage.model = "openai:gpt-4o-mini"  # an API model: the fee itself
    assert usage.line().endswith("API fees $0.00069") and usage.summary()["api_fees"] == 0.00069
    usage.model = "anthropic:unknown"
    assert usage.line().endswith("API fees not calculated (no price found for this model)")
    assert agent.Usage("openai:gpt-4o-mini").summary()["model"] == "openai:gpt-4o-mini"  # priced as the model that ran


class Reply:
    """A minimal HTTP response for urllib.request.urlopen."""

    def __init__(self, body):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self, *a):
        return self.body


def test_openai_compatible_route_settings(monkeypatch):
    monkeypatch.setenv("FAIRPRICE_MODEL_URL", "http://localhost:1234/v1/")
    settings = agent.model_settings("openai:gemma-4")
    assert settings["base_url"] == "http://localhost:1234/v1" and settings["max_tokens"] == 4096
    assert settings["timeout"] == 600 and settings["api_key"] == "not-needed" and settings["temperature"] == 0
    monkeypatch.setenv("FAIRPRICE_MODEL_API_KEY", "secret")
    assert agent.model_settings("openai:gemma-4")["api_key"] == "secret"
    assert agent.model_settings("anthropic:x") == {"temperature": 0}


def test_openai_compatible_server_checks(monkeypatch):
    monkeypatch.delenv("FAIRPRICE_MODEL_URL", raising=False)
    assert "needs FAIRPRICE_MODEL_URL" in agent.check_model("openai:gemma-4")[0]
    monkeypatch.setenv("FAIRPRICE_MODEL_URL", "http://localhost:1234/v1")
    real_find = agent.importlib.util.find_spec
    monkeypatch.setattr(agent.importlib.util, "find_spec", lambda name: None)
    assert "uv sync --extra openai-compatible" in agent.check_model("openai:gemma-4")[0]
    monkeypatch.setattr(agent.importlib.util, "find_spec", real_find)

    def refused(req, timeout):
        raise OSError("connection refused")
    monkeypatch.setattr(agent.urllib.request, "urlopen", refused)
    assert "No OpenAI-compatible server at http://localhost:1234/v1" in agent.check_model("openai:gemma-4")[0]
    seen = []

    def models(req, timeout):
        seen.append(req.headers.get("Authorization"))
        return Reply(b'{"data": [{"id": "gemma-4"}, {"id": "qwen3"}]}')
    monkeypatch.setattr(agent.urllib.request, "urlopen", models)
    monkeypatch.setenv("FAIRPRICE_MODEL_API_KEY", "k")
    assert "It has: gemma-4, qwen3" in agent.check_model("openai:llama")[0] and seen[-1] == "Bearer k"
    monkeypatch.setattr(agent, "calls_tools", lambda model: "it answered in text instead of calling a tool")
    problem = agent.check_model("openai:gemma-4")[0]
    assert "answered in text" in problem and "--enable-auto-tool-choice" in problem and "--jinja" in problem
    monkeypatch.setattr(agent, "calls_tools", lambda model: "")
    assert agent.check_model("openai:gemma-4") == []


def test_tool_calling_probe():
    class Model:
        def __init__(self, reply):
            self.reply = reply

        def bind_tools(self, tools):
            assert tools[0]["function"]["name"] == "ping"
            return self

        def invoke(self, prompt):
            if isinstance(self.reply, Exception):
                raise self.reply
            return self.reply
    assert agent.calls_tools(Model(SimpleNamespace(tool_calls=[{"name": "ping"}]))) == ""
    assert agent.calls_tools(Model(SimpleNamespace(tool_calls=[]))) == "it answered in text instead of calling a tool"
    assert "the test request failed" in agent.calls_tools(Model(RuntimeError("500")))


def test_check_shows_where_the_model_runs(monkeypatch, capsys):
    monkeypatch.setattr(agent, "check_model", lambda name: [])
    monkeypatch.setattr(agent, "MODEL", "openai:gemma-4")
    monkeypatch.delenv("FAIRPRICE_MODEL_URL", raising=False)
    agent.check_setup()
    assert "Model openai:gemma-4 at (FAIRPRICE_MODEL_URL not set)" in capsys.readouterr().out
    monkeypatch.setattr(agent, "MODEL", "anthropic:x")
    agent.check_setup()
    assert "✓ Model anthropic:x\n" in capsys.readouterr().out


def test_context_window_probe(monkeypatch, capsys):
    class Model:
        def __init__(self, reads):
            self.reads = reads

        def invoke(self, prompt):
            if isinstance(self.reads, Exception):
                raise self.reads
            return SimpleNamespace(usage_metadata={"input_tokens": self.reads})
    assert agent.context_problem(Model(7005)) == ""
    assert agent.context_problem(Model(2051)) == "it read only 2,051 of about 7,000 tokens"
    assert "the long test request failed" in agent.context_problem(Model(RuntimeError("500")))
    monkeypatch.setattr(agent, "MODEL", "openai:gemma-4")
    monkeypatch.setenv("FAIRPRICE_MODEL_URL", "http://localhost:1234/v1")
    monkeypatch.setattr(agent, "check_model", lambda name: [])
    monkeypatch.setattr(agent, "init_chat_model", lambda name, **kw: Model(2051))
    assert agent.check_setup() >= 1
    assert "The server cuts long conversations: it read only 2,051" in capsys.readouterr().out
