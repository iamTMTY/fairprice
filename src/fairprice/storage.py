"""Where reports are kept: the local disk (default) or S3-compatible object storage, e.g. MinIO on your own servers.

Reports can hold confidential bid prices: keep them in your own organisation and jurisdiction.

FAIRPRICE_STORAGE=file  (default) reports/ next to the code, or FAIRPRICE_REPORTS
FAIRPRICE_STORAGE=s3    FAIRPRICE_S3_BUCKET, optional FAIRPRICE_S3_PREFIX (default "fairprice/reports/").
                        Credentials come from the standard AWS_* settings (used by every S3-compatible store);
                        set AWS_ENDPOINT_URL to your MinIO server or other provider. Needs `uv sync --extra s3`.

Report files are flat (no folders): "DRAFT-<item>-<run>.xlsx" or "<timestamp>-<item>-<run>.xlsx" (also -prices.csv,
-sources.csv or .json). Which files belong to which report is recorded in the reports table, not read from here.
"""

import os
import re
from pathlib import Path

NAME = re.compile(r"^(DRAFT-)?[A-Za-z0-9][A-Za-z0-9._-]*\.(xlsx|csv|json)$")  # no slashes: a name never leaves the store


def check(name):
    if not NAME.match(name):
        raise ValueError(f"not a report name: {name!r}")
    return name


class LocalStorage:
    def __init__(self, root):
        self.root = Path(root)

    def save(self, name, data):
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / check(name)).write_bytes(data)

    def load(self, name):
        return (self.root / check(name)).read_bytes()

    def delete(self, name):
        (self.root / check(name)).unlink(missing_ok=True)

    def location(self, name):
        return str(self.root / check(name))


class S3Storage:
    def __init__(self, bucket, prefix):
        import boto3  # optional: uv sync --extra s3

        self.s3, self.bucket, self.prefix = boto3.client("s3"), bucket, prefix

    def save(self, name, data):
        self.s3.put_object(Bucket=self.bucket, Key=self.prefix + check(name), Body=data)

    def load(self, name):
        return self.s3.get_object(Bucket=self.bucket, Key=self.prefix + check(name))["Body"].read()

    def delete(self, name):
        self.s3.delete_object(Bucket=self.bucket, Key=self.prefix + check(name))

    def location(self, name):
        """A download link that works for one hour, so nobody needs bucket credentials to open a report."""
        return self.s3.generate_presigned_url("get_object", ExpiresIn=3600,
                                              Params={"Bucket": self.bucket, "Key": self.prefix + check(name)})


def storage():
    if os.environ.get("FAIRPRICE_STORAGE", "file") == "s3":
        return S3Storage(os.environ["FAIRPRICE_S3_BUCKET"], os.environ.get("FAIRPRICE_S3_PREFIX", "fairprice/reports/"))
    root = Path(__file__).resolve().parents[2] / os.environ.get("FAIRPRICE_REPORTS", "reports")  # relative to the project
    return LocalStorage(root)
