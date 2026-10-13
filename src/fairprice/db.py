"""The SQL database: the audit log, the reports and human approvals. Any SQLAlchemy URL works (FAIRPRICE_SQL_URL), e.g.
postgresql+psycopg://user@host/db with that database's driver installed. Default: data/runs.sqlite.
"""

import datetime
import functools
import json
import os
import secrets
from pathlib import Path

from sqlalchemy import JSON, Column, DateTime, Integer, MetaData, String, Table, create_engine, select

ROOT = Path(__file__).resolve().parents[2]
META = MetaData()
AUDIT_LOG = Table("audit_log", META,
                  Column("id", Integer, primary_key=True, autoincrement=True),
                  Column("ts", DateTime(timezone=True), nullable=False),
                  Column("run_id", String(64), index=True),
                  Column("event", String(64), nullable=False),  # tool_call, run_started, human_decision, ...
                  Column("detail", JSON))  # tool inputs and outputs, the decision, the approver's name
REPORTS = Table("reports", META,
                Column("id", Integer, primary_key=True, autoincrement=True),
                Column("name", String(200), unique=True, nullable=False),  # file name without extension
                Column("run_id", String(64), index=True),
                Column("status", String(16), nullable=False),  # draft or approved
                Column("format", String(8), nullable=False),  # xlsx, csv or json
                Column("files", JSON),  # the file names written in the report store
                Column("approved_by", String(200)),
                Column("created_at", DateTime(timezone=True), nullable=False),
                Column("approved_at", DateTime(timezone=True)),
                Column("data", JSON, nullable=False))  # the report itself: summary, prices, sources

APPROVALS = Table("approvals", META,
                  Column("id", String(32), primary_key=True),
                  Column("action", String(32), nullable=False),  # write_price_review or add_country
                  Column("target", String(200), nullable=False),  # the report name, or the country code
                  Column("status", String(16), nullable=False),  # pending, approved, rejected or used
                  Column("approved_by", String(200)),
                  Column("created_at", DateTime(timezone=True), nullable=False),
                  Column("decided_at", DateTime(timezone=True)))


def url():
    value = os.environ.get("FAIRPRICE_SQL_URL", "sqlite:///data/runs.sqlite")
    if value.startswith("sqlite:///"):  # a relative SQLite path is relative to the project, not the current folder
        value = f"sqlite:///{ROOT / value.removeprefix('sqlite:///')}"
    return value


def engine():
    return _engine(url())


@functools.cache
def _engine(address):
    if address.startswith("sqlite:///"):
        Path(address.removeprefix("sqlite:///")).parent.mkdir(parents=True, exist_ok=True)
    made = create_engine(address, json_serializer=lambda o: json.dumps(o, default=str, ensure_ascii=False))
    META.create_all(made)
    return made


# Human approvals. Only a person grants one: at the agent's approval menu, or with `fairprice --approve <id>`.
# An MCP tool that changes something needs an approved, unused approval for exactly its action and target.

def now():
    return datetime.datetime.now(datetime.timezone.utc)


def request_approval(action, target):
    """A pending request for a person to decide; the same pending request is reused if asked again."""
    with engine().begin() as db:
        found = db.execute(select(APPROVALS.c.id).where(APPROVALS.c.action == action, APPROVALS.c.target == target,
                                                        APPROVALS.c.status == "pending")).first()
        if found:
            return found.id
        new = secrets.token_hex(8)
        db.execute(APPROVALS.insert().values(id=new, action=action, target=target, status="pending", created_at=now()))
        return new


def grant(action, target, approved_by):
    """An approval given by a person in the terminal (the agent's approval menu)."""
    new = secrets.token_hex(8)
    with engine().begin() as db:
        db.execute(APPROVALS.insert().values(id=new, action=action, target=target, status="approved",
                                             approved_by=approved_by, created_at=now(), decided_at=now()))
    return new


def decide(approval_id, approved, approved_by=None):
    """A person's decision on a pending request."""
    with engine().begin() as db:
        db.execute(APPROVALS.update().where(APPROVALS.c.id == approval_id, APPROVALS.c.status == "pending").values(
            status="approved" if approved else "rejected", approved_by=approved_by, decided_at=now()))


def use_approval(approval_id, action, target):
    """Spend an approval: returns its row if it is approved, unused and for exactly this action and target."""
    with engine().begin() as db:
        row = db.execute(select(APPROVALS).where(APPROVALS.c.id == approval_id, APPROVALS.c.action == action,
                                                 APPROVALS.c.target == target, APPROVALS.c.status == "approved")).first()
        if row:
            db.execute(APPROVALS.update().where(APPROVALS.c.id == approval_id).values(status="used"))
        return row


def approvals(status="pending"):
    with engine().connect() as db:
        return db.execute(select(APPROVALS).where(APPROVALS.c.status == status).order_by(APPROVALS.c.created_at)).all()
