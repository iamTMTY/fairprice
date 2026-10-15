"""FairPrice as a web server: access keys, organisations, the audit log, uploads, signed report links, the
background review and the CLI as a thin client. Over HTTP with Starlette's test client."""

import argparse
import asyncio
import base64
import json
import time
from pathlib import Path
from unittest import mock

import pytest
from sqlalchemy import select
from starlette.testclient import TestClient

from conftest import ScriptedModel, fake_session, ids
from fairprice import agent, db, server

EXAMPLES = Path(__file__).resolve().parents[1] / "evals" / "packages"  # the eval's 3-line package
HILUX = "SELECT id FROM lines WHERE description LIKE 'Supply of Toyota Hilux double cabin pickup unit%' ORDER BY id"
APP = None


@pytest.fixture(scope="module")
def web():
    """One HTTP app for the module (its session manager can start once), with keys for two organisations."""
    global APP
    APP = APP or server.mcp.streamable_http_app()
    keys = {"app": db.add_key("acme", "Acme portal"), "other": db.add_key("other", "Other org")}
    with TestClient(APP, base_url="http://127.0.0.1:8000") as client:
        yield client, keys


def rpc(client, key, method, params=None):
    reply = client.post("/mcp", headers={"Authorization": f"Bearer {key}", "Accept": "application/json, text/event-stream"},
                        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}})
    return reply.status_code, reply.json() if reply.status_code == 200 else None


def call(client, key, name, **arguments):
    status, body = rpc(client, key, "tools/call", {"name": name, "arguments": arguments})
    assert status == 200, status
    return json.loads(body["result"]["content"][0]["text"])


def checked_item(client, key):
    run = call(client, key, "start_review", item="Toyota Hilux double cabin pickup", unit_price=95e6, year=2025)["run_id"]
    call(client, key, "find_comparables", run_id=run, keywords=["hilux"])
    call(client, key, "check_price_comparability", run_id=run, candidate_ids=ids(HILUX))
    return run


def test_every_call_needs_a_key(web):
    client, keys = web
    assert rpc(client, "", "tools/list")[0] == 401
    assert rpc(client, "fp_nope_nope", "tools/list")[0] == 401
    status, body = rpc(client, keys["app"], "tools/list")
    names = {t["name"] for t in body["result"]["tools"]}
    assert {"start_package_review", "review_with_fairprice", "review_status", "write_price_review"} <= names
    status, body = rpc(client, keys["app"], "resources/read", {"uri": "fairprice://bid-package/schema"})
    assert json.loads(body["result"]["contents"][0]["text"])["title"] == "FairPrice bid package"


def test_organisations_see_only_their_own_reviews_and_every_call_is_audited(web):
    client, keys = web
    run = checked_item(client, keys["app"])
    assert "No review" in call(client, keys["other"], "review_status", run_id=run)["error"]
    assert "No review" in call(client, keys["other"], "write_price_review", run_id=run, approved_by="X")["error"]
    with db.engine().connect() as conn:
        rows = conn.execute(select(db.AUDIT_LOG).where(db.AUDIT_LOG.c.run_id == run).order_by(db.AUDIT_LOG.c.id)).all()
    assert [(r.org, r.detail["tool"], r.detail["caller"]) for r in rows][:2] == [
        ("acme", "start_review", "Acme portal"), ("acme", "find_comparables", "Acme portal")]
    assert ("other", "Other org") in {(r.org, r.detail["caller"]) for r in rows}  # the other org's attempt, under its name


def test_a_workbook_uploaded_through_mcp_is_read_like_a_file(web):
    client, keys = web
    upload = base64.b64encode((EXAMPLES / "bid-package.xlsx").read_bytes()).decode()
    started = call(client, keys["app"], "start_package_review", file=upload, filename="package.xlsx", year=2025)
    assert started["bidders"] == ["Bidder A", "Bidder B", "Bidder C"] and started["bid_file_problems"] == 4
    token = db.CALLER.set({"org": "acme", "role": "app", "name": "x"})
    try:
        assert "sheet “Bidder B”" in db.get_run(started["run_id"]).checks[0]["where"]
    finally:
        db.CALLER.reset(token)
    assert call(client, keys["app"], "start_package_review", file="%%%", filename="x.xlsx")["problems"] == ["file is not base64"]
    assert "Send package" in call(client, keys["app"], "start_package_review")["error"]
    bad = base64.b64encode(b"Bidder,Item No,Price per piece\n").decode()
    assert "unknown column" in call(client, keys["app"], "start_package_review", file=bad, filename="t.csv")["problems"][0]


def test_a_report_is_written_under_the_name_the_app_sends_and_downloaded_by_link(web, monkeypatch):
    client, keys = web
    monkeypatch.setenv("FAIRPRICE_PUBLIC_URL", "http://127.0.0.1:8000")
    run = checked_item(client, keys["app"])
    assert "full name" in call(client, keys["app"], "write_price_review", run_id=run, approved_by="")["error"]
    saved = call(client, keys["app"], "write_price_review", run_id=run, approved_by="Ada Obi", format="csv")
    assert saved["approved_by"] == "Ada Obi" and saved["files"][0].startswith("http://127.0.0.1:8000/reports/")
    report = call(client, keys["app"], "get_price_review", run_id=run)
    assert report["report"]["approved_by"] == "Ada Obi"
    link = report["files"][0].removeprefix("http://127.0.0.1:8000")
    assert client.get(link).content.startswith(b"\xef\xbb\xbfStatus")
    assert client.get(link[:-2] + "00").status_code == 403
    assert client.get(link.split("?")[0] + "?expires=1&sig=x").status_code == 403
    expires = int(time.time()) + 60
    gone = f"/reports/gone-x.csv?expires={expires}&sig={server.sign('gone-x.csv', expires)}"
    assert client.get(gone).status_code == 404
    assert client.get("/template.xlsx").content[:2] == b"PK"


def test_fairprices_own_review_runs_in_the_background(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "ROOT", tmp_path)  # the review's log goes to <ROOT>/data/reviews
    run = server.start_review("Toyota Hilux double cabin pickup", 95e6, year=2025)["run_id"]
    assert server.review_status(run)["state"] == "not started"
    assert "No review" in server.review_with_fairprice("nope")["error"] and "No review" in server.review_status("nope")["error"]
    with mock.patch("subprocess.Popen", return_value=mock.Mock(pid=999999)) as popen:
        started = server.review_with_fairprice(run)
    assert started["started"] == run and popen.call_args.args[0][-2:] == ["--review", run]
    assert popen.call_args.kwargs["env"]["FAIRPRICE_ORG"] == "local"
    assert server.review_status(run)["state"] == "failed"  # pid 999999 is not running: it ended without finishing
    db.add_step(run, "*", "status", {"state": "running", "pid": __import__("os").getpid()})
    assert "already running" in server.review_with_fairprice(run)["error"]
    model = ScriptedModel(keywords={"Toyota Hilux double cabin pickup": ["hilux"]})
    monkeypatch.setattr(agent, "session", lambda *a, **k: fake_session(model))
    asyncio.run(agent.review_unattended(run))
    status = server.review_status(run)
    assert status["state"] == "done" and "₦" in status["summary"] and "write_price_review" in status["next"]
    assert status["lines_checked"] == "1 of 1"
    with pytest.raises(SystemExit, match="No review"):
        asyncio.run(agent.review_unattended("nope"))

    def broken(*a, **k):
        raise RuntimeError("model went away")
    monkeypatch.setattr(agent, "session", broken)
    with pytest.raises(RuntimeError):
        asyncio.run(agent.review_unattended(run))
    assert server.review_status(run)["state"] == "failed"


def test_the_cli_as_a_thin_client(monkeypatch, capsys, tmp_path, answers):
    replies = {"start_review": {"run_id": "r1", "lines": [{}], "country": "NG", "currency": "NGN"},
               "list_countries": {"countries": [{"code": "NG", "name": "Nigeria", "loaded": True},
                                                {"code": "GH", "name": "Ghana", "loaded": False}]},
               "review_with_fairprice": {"started": "r1"},
               "write_price_review": {"saved": "r1-report", "approved_by": "Ada Obi"},
               "review_status": iter([{"state": "running", "lines_checked": "0 of 1"},
                                      {"state": "done", "lines_checked": "1 of 1", "summary": "Line 1 ok"}])}
    seen = []

    class Tool:
        def __init__(self, name):
            self.name = name

        async def ainvoke(self, args):
            seen.append((self.name, args))
            reply = replies[self.name]
            return json.dumps(next(reply) if hasattr(reply, "__next__") else reply)

    class Client:
        def __init__(self, servers):
            assert servers["fairprice"]["headers"]["Authorization"] == "Bearer k"

        async def get_tools(self):
            return [Tool(n) for n in replies] + [Tool("start_package_review")]
    monkeypatch.setattr(agent, "MultiServerMCPClient", Client)
    monkeypatch.setattr(agent, "POLL_SECONDS", 0)
    monkeypatch.setenv("FAIRPRICE_SERVER_URL", "http://x/")
    monkeypatch.setenv("FAIRPRICE_API_KEY", "k")
    monkeypatch.setattr("sys.argv", ["fairprice", "laptop", "--unit-price", "5"])
    answers(["approve", "Ada Obi"])
    agent.main()
    out = capsys.readouterr().out
    assert "Review r1 on http://x" in out and "1 of 1 lines checked" in out and '"approved_by": "Ada Obi"' in out
    assert "Country: NG · prices in NGN" in out  # the only country loaded on the server
    assert seen[-1] == ("write_price_review", {"run_id": "r1", "approved_by": "Ada Obi"})
    replies["review_status"] = iter([{"state": "done", "lines_checked": "1 of 1", "summary": "ok"}])
    answers(["reject"])
    agent.main()
    assert "Not saved: the reviewer rejected it." in capsys.readouterr().out
    replies["start_package_review"] = {"error": "The package does not match", "problems": ["row 2: bad"]}
    (tmp_path / "t.csv").write_text("x")
    with pytest.raises(SystemExit, match="row 2: bad"):
        asyncio.run(agent.remote(argparse.Namespace(sheet=str(tmp_path / "t.csv"), currency=None, year=2025, country="NG")))
    assert seen[-1][1]["file"] == base64.b64encode(b"x").decode()
    replies["start_review"] = {"run_id": "r2", "lines": [{}], "country": "NG", "currency": "NGN"}
    replies["review_status"] = iter([{"state": "failed", "error": "boom"}])
    with pytest.raises(SystemExit, match="The review failed: boom"):
        asyncio.run(agent.remote(argparse.Namespace(sheet=None, item="x", unit_price=1, currency=None, year=2025,
                                                    country="NG")))
    monkeypatch.setattr("sys.argv", ["fairprice", "laptop"])
    with pytest.raises(SystemExit):
        agent.main()


def test_access_keys_and_the_server_command(capsys):
    key = db.add_key("acme", "Portal")
    key_id = key.split("_")[1]
    assert db.check_key(key)["org"] == "acme" and db.check_key(key, "admin") is None
    assert db.check_key("nonsense") is None and db.check_key(key[:-3] + "xyz") is None
    server.main(["--revoke-key", key_id])
    assert "Revoked." in capsys.readouterr().out and db.check_key(key) is None
    server.main(["--revoke-key", key_id])
    assert "No such active key." in capsys.readouterr().out
    server.main(["--add-key", "acme", "--name", "Portal two"])
    assert "Shown once" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        server.main(["--add-key", "acme"])
    with mock.patch.object(server.mcp, "run") as run:
        server.main(["--http", "--port", "9999"])
    assert run.call_args.kwargs == {"transport": "streamable-http"} and server.mcp.settings.port == 9999


def test_a_database_from_before_organisations_is_upgraded(tmp_path, monkeypatch):
    import sqlite3
    path = tmp_path / "old.sqlite"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE runs (id VARCHAR(32) PRIMARY KEY, kind VARCHAR(16) NOT NULL, title VARCHAR(300) NOT NULL,"
                     " request JSON NOT NULL, lines JSON NOT NULL, bidders JSON NOT NULL, checks JSON,"
                     " created_at DATETIME NOT NULL)")
        conn.execute("INSERT INTO runs VALUES ('old1', 'item', 't', '{}', '[]', '[]', NULL, '2026-01-01')")
    monkeypatch.setenv("FAIRPRICE_SQL_URL", f"sqlite:///{path}")
    assert db.get_run("old1").org == "local"  # the old run belongs to this computer
