"""Report storage (local folder and S3-compatible) and the database settings."""

import os
from unittest import mock

import boto3
import pytest
from moto import mock_aws

from fairprice import db, storage


def exercise(store):
    store.save("DRAFT-a-1.xlsx", b"one")
    assert store.load("DRAFT-a-1.xlsx") == b"one"
    assert "DRAFT-a-1.xlsx" in store.location("DRAFT-a-1.xlsx")
    store.delete("DRAFT-a-1.xlsx")
    with pytest.raises(Exception):
        store.load("DRAFT-a-1.xlsx")


def test_local_storage(tmp_path):
    exercise(storage.LocalStorage(tmp_path / "reports"))


def test_s3_storage():
    env = {"AWS_ACCESS_KEY_ID": "x", "AWS_SECRET_ACCESS_KEY": "x", "AWS_DEFAULT_REGION": "us-east-1",
           "FAIRPRICE_STORAGE": "s3", "FAIRPRICE_S3_BUCKET": "fp-test"}
    with mock.patch.dict(os.environ, env), mock_aws():
        boto3.client("s3").create_bucket(Bucket="fp-test")
        store = storage.storage()
        assert isinstance(store, storage.S3Storage)
        exercise(store)
        assert store.location("x.json").startswith("https://")


def test_names_can_never_leave_the_store():
    for bad in ("../x.xlsx", "a/b.xlsx", "x.exe", "", ".hidden.xlsx"):
        with pytest.raises(ValueError):
            storage.check(bad)
    assert storage.check("DRAFT-x-prices.csv") == "DRAFT-x-prices.csv"


def test_default_storage_is_the_reports_folder(monkeypatch):
    monkeypatch.setenv("FAIRPRICE_STORAGE", "file")
    monkeypatch.setenv("FAIRPRICE_REPORTS", "relative/reports")
    assert storage.storage().root == storage.Path(storage.__file__).resolve().parents[2] / "relative" / "reports"


def test_sql_url_relative_paths_are_project_relative(monkeypatch):
    monkeypatch.setenv("FAIRPRICE_SQL_URL", "sqlite:///data/x.sqlite")
    assert db.url() == f"sqlite:///{db.ROOT / 'data' / 'x.sqlite'}"
    monkeypatch.setenv("FAIRPRICE_SQL_URL", "postgresql+psycopg://u@h/d")
    assert db.url() == "postgresql+psycopg://u@h/d"
