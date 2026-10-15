"""The SQL database: runs and their steps, the audit log, the reports and access keys. Any SQLAlchemy URL works (FAIRPRICE_SQL_URL), e.g.
postgresql+psycopg://user@host/db with that database's driver installed. Default: data/runs.sqlite.
"""

import contextvars
import datetime
import functools
import hashlib
import hmac
import json
import os
import secrets
from pathlib import Path

from sqlalchemy import JSON, Column, DateTime, Integer, MetaData, String, Table, create_engine, inspect, select, text

# Who is calling: the organisation and role of the access key on this request (a web deployment), or None for
# this computer's own CLI and tools (organisation "local", or FAIRPRICE_ORG for a review run on an
# organisation's behalf). Every run, report and audit row belongs to one organisation, and a caller
# sees only its own.
CALLER = contextvars.ContextVar("caller", default=None)


def org():
    caller = CALLER.get()
    return caller["org"] if caller else os.environ.get("FAIRPRICE_ORG", "local")


def org_column():
    """The column every scoped table has; rows from before organisations existed belong to "local"."""
    return Column("org", String(64), nullable=False, server_default="local", index=True)


ROOT = Path(__file__).resolve().parents[2]
META = MetaData()
AUDIT_LOG = Table("audit_log", META,
                  Column("id", Integer, primary_key=True, autoincrement=True),
                  Column("ts", DateTime(timezone=True), nullable=False),
                  Column("run_id", String(64), index=True),
                  Column("event", String(64), nullable=False),  # tool_call, run_started, human_decision, ...
                  Column("detail", JSON),  # tool inputs and outputs, the decision, the approver's name
                  org_column())
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
                Column("data", JSON, nullable=False),  # the report itself: summary, prices, sources
                org_column())

RUNS = Table("runs", META,
             Column("id", String(32), primary_key=True),
             Column("kind", String(16), nullable=False),  # item (one price) or package (a BoQ and its bids)
             Column("title", String(300), nullable=False),
             Column("request", JSON, nullable=False),  # currency, year, country
             Column("lines", JSON, nullable=False),  # [{line, item, quantity, prices: {bidder: unit price}}]
             Column("bidders", JSON, nullable=False),
             Column("checks", JSON),  # problems found in the bid files (packages only)
             Column("created_at", DateTime(timezone=True), nullable=False),
             org_column())
RUN_STEPS = Table("run_steps", META,
                  Column("id", Integer, primary_key=True, autoincrement=True),
                  Column("run_id", String(32), index=True, nullable=False),
                  Column("line", String(64), nullable=False),
                  Column("kind", String(16), nullable=False),  # plan, search, check, challenge, cost
                  Column("data", JSON, nullable=False),  # what was asked and what our code found
                  Column("created_at", DateTime(timezone=True), nullable=False))
KEYS = Table("access_keys", META,  # for a web deployment: who may call the tools, and who may approve
             Column("id", String(16), primary_key=True),  # the public part of the key
             Column("hash", String(64), nullable=False),  # sha256 of the secret part; the secret is never stored
             Column("org", String(64), nullable=False),
             Column("role", String(16), nullable=False),  # "app": calls the tools
             Column("name", String(200), nullable=False),  # the app's name, shown in the audit log
             Column("created_at", DateTime(timezone=True), nullable=False),
             Column("revoked_at", DateTime(timezone=True)))


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
    with made.begin() as db:  # columns added since a table was created (only nullable or defaulted ones)
        for table in META.sorted_tables:
            have = {c["name"] for c in inspect(db).get_columns(table.name)}
            for col in table.columns:
                if col.name not in have:
                    default = f" DEFAULT '{col.server_default.arg}'" if col.server_default is not None else ""
                    db.execute(text(f"ALTER TABLE {table.name} ADD COLUMN {col.name} "
                                    f"{col.type.compile(made.dialect)}{default}"))
    return made


def now():
    return datetime.datetime.now(datetime.timezone.utc)


def audit(run_id, **event):
    """Append one row to the audit log, under the caller's organisation. A row without an `event` is a tool call."""
    with engine().begin() as db:
        db.execute(AUDIT_LOG.insert().values(ts=now(), run_id=run_id, event=event.pop("event", "tool_call"),
                                             detail=event, org=org()))


def add_key(org_name, name, role="app"):
    """A new access key; the only time its secret is shown. Store it like a password."""
    key_id, secret = secrets.token_hex(6), secrets.token_urlsafe(24)
    with engine().begin() as db:
        db.execute(KEYS.insert().values(id=key_id, hash=hashlib.sha256(secret.encode()).hexdigest(), org=org_name,
                                        role=role, name=name, created_at=now()))
    return f"fp_{key_id}_{secret}"


def check_key(key, role="app"):
    """The caller ({org, role, name, key}) for a valid, unrevoked key of this role, else None."""
    parts = str(key).split("_", 2)
    if len(parts) != 3 or parts[0] != "fp":
        return None
    with engine().connect() as db:
        row = db.execute(select(KEYS).where(KEYS.c.id == parts[1], KEYS.c.revoked_at.is_(None))).first()
    if row is None or row.role != role or not hmac.compare_digest(row.hash, hashlib.sha256(parts[2].encode()).hexdigest()):
        return None
    return {"org": row.org, "role": row.role, "name": row.name, "key": row.id}


def revoke_key(key_id):
    with engine().begin() as db:
        return db.execute(KEYS.update().where(KEYS.c.id == key_id, KEYS.c.revoked_at.is_(None))
                          .values(revoked_at=now())).rowcount


# Runs: one review across many MCP calls. Every step's real data comes from our code and is stored here,
# so a report is built only from what this run recorded.

def start_run(kind, title, request, lines, bidders, checks=None):
    new = secrets.token_hex(6)
    with engine().begin() as db:
        db.execute(RUNS.insert().values(id=new, kind=kind, title=title, request=request, lines=lines, bidders=bidders,
                                        checks=checks, created_at=now(), org=org()))
    return new


def get_run(run_id):
    with engine().connect() as db:
        return db.execute(select(RUNS).where(RUNS.c.id == run_id, RUNS.c.org == org())).first()


def add_step(run_id, line, kind, data):
    with engine().begin() as db:
        return db.execute(RUN_STEPS.insert().values(run_id=run_id, line=line, kind=kind, data=data,
                                                    created_at=now())).inserted_primary_key[0]


def steps(run_id, line=None, kind=None):
    query = select(RUN_STEPS).where(RUN_STEPS.c.run_id == run_id)
    if line is not None:
        query = query.where(RUN_STEPS.c.line == line)
    if kind is not None:
        query = query.where(RUN_STEPS.c.kind == kind)
    with engine().connect() as db:
        return db.execute(query.order_by(RUN_STEPS.c.id)).all()
