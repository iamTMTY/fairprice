"""The eval runner: judging, single and package cases, the model-free checks, and the results file."""

import asyncio
import json
from types import SimpleNamespace

import pytest

from conftest import ScriptedModel, fake_session, ids
from fairprice import evals


def test_judge():
    case = {"expect": ["within_band"], "traps": [7], "max_accepted": 1}
    good = {"final": {"assessment": "within_band", "accepted": [{"id": 1}], "band": {"median": 5}},
            "challenge": {"verdict": "STANDS"}}
    assert evals.judge(case, good)["passed"]
    bad = {"final": {"assessment": "above_band: 2x", "accepted": [{"id": 7}, {"id": 8}]}, "saved": "x"}
    failures = evals.judge(case, bad)["failures"]
    assert len(failures) == 4 and "trap records in band: [7]" in failures


def test_model_free_checks_pass(monkeypatch):
    monkeypatch.setattr(evals, "KNOWN_RECORD", ids("SELECT id FROM lines WHERE unit_price IS NOT NULL LIMIT 1")[0])
    results = evals.deterministic_checks()
    assert all(r["passed"] for r in results), [r for r in results if not r["passed"]]
    assert len(results) >= 15


def test_run_cases_and_write_results(tmp_path, monkeypatch, capsys):
    cases = [{"id": "hilux", "item": "Toyota Hilux double cabin pickup", "unit_price": 95e6, "year": 2025,
              "expect": ["within_band"], "traps": []},
             {"id": "pkg", "package": "examples/bid-package.json", "year": 2025,
              "expect_cells": {"1/Bidder B": ["above_band"], "3/Bidder C": ["not_priced"]}},
             {"id": "broken", "package": "examples/missing.json", "year": 2025, "expect_cells": {}}]
    (tmp_path / "cases.json").write_text(json.dumps(cases))
    monkeypatch.setattr(evals, "CASES", tmp_path / "cases.json")
    monkeypatch.setattr(evals, "RESULTS", tmp_path / "results")
    monkeypatch.setattr(evals, "KNOWN_RECORD", ids("SELECT id FROM lines WHERE unit_price IS NOT NULL LIMIT 1")[0])
    model = ScriptedModel(keywords={"Toyota Hilux double cabin pickup": ["hilux"]})
    monkeypatch.setattr(evals.agent, "session", lambda *a: fake_session(model))
    asyncio.run(evals.main_async(SimpleNamespace(cases=None, repeats=1, model="scripted")))
    out = capsys.readouterr().out
    assert "hilux                  run 1: PASS" in out and "pkg                    run 1: PASS" in out
    assert "broken                 run 1: FAIL error" in out
    written = [json.loads(line) for line in next((tmp_path / "results").glob("*.jsonl")).read_text().splitlines()]
    assert [r["case"] for r in written] == ["hilux", "pkg", "broken"]
    assert all({"model_calls", "input_tokens", "output_tokens"} <= set(r) for r in written)


def test_a_crashing_case_is_a_failed_run_not_a_crash():
    class Broken:
        async def ainvoke(self, *a, **k):
            raise RuntimeError("model went away")
    result = asyncio.run(evals.run_case(Broken(), {"id": "x", "item": "y", "unit_price": 1, "year": 2025,
                                                    "expect": [], "traps": []}, "m"))
    assert result["assessment"] == "error" and not result["passed"]


def test_main_parses_arguments(monkeypatch):
    seen = []

    async def fake(args):
        seen.append((args.repeats, args.model, args.cases))
    monkeypatch.setattr(evals, "main_async", fake)
    monkeypatch.setattr("sys.argv", ["fairprice-evals", "--repeats", "1", "--cases", "hilux"])
    evals.main()
    assert seen == [(1, evals.agent.MODEL, "hilux")]


def test_filter_cases(tmp_path, monkeypatch, capsys):
    (tmp_path / "cases.json").write_text(json.dumps([{"id": "a"}, {"id": "b"}]))
    monkeypatch.setattr(evals, "CASES", tmp_path / "cases.json")
    monkeypatch.setattr(evals, "RESULTS", tmp_path / "results")
    monkeypatch.setattr(evals, "deterministic_checks", lambda: [])
    seen = []

    async def run_case(graph, case, model):
        seen.append(case["id"])
        return {"case": case["id"], "run_id": "r", "seconds": 0.1, "assessment": "x", "n": 0, "median": None,
                "challenge": None, "failures": [], "passed": True}
    monkeypatch.setattr(evals, "run_case", run_case)
    monkeypatch.setattr(evals.agent, "session", lambda *a: fake_session(ScriptedModel()))
    asyncio.run(evals.main_async(SimpleNamespace(cases="b", repeats=2, model="m")))
    assert seen == ["b", "b"]


@pytest.mark.parametrize("fmt", ["xlsx", "csv", "json"])
def test_round_trip_in_each_format(fmt, monkeypatch):
    monkeypatch.setattr(evals, "KNOWN_RECORD", ids("SELECT id FROM lines WHERE unit_price IS NOT NULL LIMIT 1")[0])
    assert evals.round_trip("file", fmt)["passed"]
