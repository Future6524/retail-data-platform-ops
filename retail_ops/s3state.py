"""Versioned-S3 checkpoint for the DuckDB warehouse + a lease lock, both using S3 conditional writes.

Why: a Fargate task's disk is ephemeral. Each scheduled run (1) takes a lease lock, (2) downloads the warehouse
file and remembers its ETag, (3) runs the pipeline, (4) uploads the file with ``If-Match: <etag>`` (or
``If-None-Match: *`` for the very first upload). If anything else wrote the object in between, S3 answers 412 and we
REFUSE to overwrite (the run is reported failed; the next run starts from the newer state). The bucket is versioned,
so every committed state is also recoverable.

Clients are duck-typed (boto3 S3 client API) and injected, so tests use an in-memory fake and this module imports
neither boto3 nor botocore. ``make_s3_client`` is the only place boto3 is imported.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from .events import Clock, utc_now

CONFLICT_CODES = {"PreconditionFailed", "ConditionalRequestConflict", "412", "409"}
MISSING_CODES = {"NoSuchKey", "404", "NotFound"}
SAFE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
ALLOWED_SUFFIXES = (".csv", ".json", ".jsonl", ".ndjson")
CHUNK = 1024 * 1024


class StateConflict(RuntimeError):
    """The object changed (or appeared) since we read it; we did not overwrite it."""


class LockHeld(RuntimeError):
    """Another run holds an unexpired lease."""


def error_code(exc: BaseException) -> str:
    resp = getattr(exc, "response", None)
    if isinstance(resp, dict):
        return str(resp.get("Error", {}).get("Code", ""))
    return ""


def make_s3_client() -> Any:  # pragma: no cover - needs boto3 + real credentials; never used in tests
    import boto3
    return boto3.client("s3")


class StateStore:
    def __init__(self, client: Any, bucket: str, key: str):
        self.client, self.bucket, self.key = client, bucket, key

    def download(self, dest: Path) -> str | None:
        """Fetch the state file to ``dest``; returns its ETag, or None if no state exists yet."""
        try:
            resp = self.client.get_object(Bucket=self.bucket, Key=self.key)
        except Exception as exc:  # noqa: BLE001
            if error_code(exc) in MISSING_CODES:
                return None
            raise
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(dest.name + ".part")
        with tmp.open("wb") as fh:
            body = resp["Body"]
            while chunk := body.read(CHUNK):
                fh.write(chunk)
        tmp.replace(dest)
        return str(resp["ETag"])

    def upload(self, src: Path, expected_etag: str | None) -> str:
        """Conditionally replace the state. Raises StateConflict if it changed since ``expected_etag``."""
        cond = {"IfNoneMatch": "*"} if expected_etag is None else {"IfMatch": expected_etag}
        try:
            with src.open("rb") as fh:
                resp = self.client.put_object(Bucket=self.bucket, Key=self.key, Body=fh,
                                              ChecksumAlgorithm="SHA256", **cond)
        except Exception as exc:  # noqa: BLE001
            if error_code(exc) in CONFLICT_CODES:
                raise StateConflict(f"state object changed concurrently; not overwritten ({error_code(exc)})") from exc
            raise
        return str(resp["ETag"])


class LeaseLock:
    """Advisory lease in S3. Acquire = conditional create; expired leases are taken over with If-Match.

    Lock is a *courtesy* that avoids wasted parallel work; correctness comes from StateStore.upload's If-Match.
    No heartbeat: pick ttl_seconds comfortably above the task timeout.
    """

    def __init__(self, client: Any, bucket: str, key: str, owner: str, ttl_seconds: int = 7200,
                 clock: Clock = utc_now):
        self.client, self.bucket, self.key, self.owner = client, bucket, key, owner
        self.ttl, self.clock, self.etag = ttl_seconds, clock, None

    def _body(self) -> bytes:
        now = self.clock()
        return json.dumps({"owner": self.owner, "acquired": now.isoformat(),
                           "expires": (now + timedelta(seconds=self.ttl)).isoformat()}).encode()

    def acquire(self) -> None:
        try:
            self.etag = self.client.put_object(Bucket=self.bucket, Key=self.key, Body=self._body(),
                                               IfNoneMatch="*")["ETag"]
            return
        except Exception as exc:  # noqa: BLE001
            if error_code(exc) not in CONFLICT_CODES:
                raise
        try:
            cur = self.client.get_object(Bucket=self.bucket, Key=self.key)
        except Exception as exc:  # noqa: BLE001
            if error_code(exc) in MISSING_CODES:
                raise LockHeld("lock vanished while acquiring; retry the run") from exc
            raise
        info = json.loads(cur["Body"].read() or b"{}")
        try:
            expired = datetime.fromisoformat(info["expires"]) <= self.clock()
        except (KeyError, ValueError, TypeError):
            expired = False  # unreadable lock: do not guess, fail safe
        if not expired:
            raise LockHeld(f"another run holds the lock (owner={info.get('owner')!r}, expires={info.get('expires')})")
        try:
            self.etag = self.client.put_object(Bucket=self.bucket, Key=self.key, Body=self._body(),
                                               IfMatch=cur["ETag"])["ETag"]
        except Exception as exc:  # noqa: BLE001
            if error_code(exc) in CONFLICT_CODES:
                raise LockHeld("lost the race to take over an expired lock") from exc
            raise

    def release(self) -> None:
        """Delete the lock only if it is still ours (If-Match on our ETag); never raises on conflict."""
        if self.etag is None:
            return
        try:
            self.client.delete_object(Bucket=self.bucket, Key=self.key, IfMatch=self.etag)
        except Exception as exc:  # noqa: BLE001
            if error_code(exc) not in CONFLICT_CODES | MISSING_CODES:
                raise
        finally:
            self.etag = None


def safe_input_name(key: str, prefix: str) -> str | None:
    """Map an S3 key under ``prefix`` to a safe local file name, or None if it must be skipped."""
    if not key.startswith(prefix):
        return None
    name = key[len(prefix):]
    if "/" in name or name in (".", "..") or not SAFE_NAME.fullmatch(name) or ".." in name:
        return None
    return name if name.lower().endswith(ALLOWED_SUFFIXES) else None


@dataclass
class InputSync:
    downloaded: list[str]
    skipped: list[str]


def sync_inputs(client: Any, bucket: str, prefix: str, dest: Path, *, max_files: int = 500,
                max_bytes: int = 512 * 1024 * 1024) -> InputSync:
    dest.mkdir(parents=True, exist_ok=True)
    root = dest.resolve()
    got: list[str] = []
    skipped: list[str] = []
    token: str | None = None
    while True:
        kw: dict[str, Any] = {"Bucket": bucket, "Prefix": prefix}
        if token:
            kw["ContinuationToken"] = token
        page = client.list_objects_v2(**kw)
        for obj in page.get("Contents", []):
            key = obj["Key"]
            name = safe_input_name(key, prefix)
            if name is None or obj.get("Size", 0) > max_bytes:
                if not key.endswith("/"):
                    skipped.append(key)
                continue
            if len(got) >= max_files:
                raise RuntimeError(f"more than {max_files} input files under {prefix!r}; refusing")
            target = (root / name).resolve()
            if target.parent != root:  # belt and braces on top of safe_input_name
                skipped.append(key)
                continue
            body = client.get_object(Bucket=bucket, Key=key)["Body"]
            with target.open("wb") as fh:
                while chunk := body.read(CHUNK):
                    fh.write(chunk)
            got.append(name)
        if not page.get("IsTruncated"):
            return InputSync(sorted(got), sorted(skipped))
        token = page.get("NextContinuationToken")


def put_report(client: Any, bucket: str, key: str, report: dict[str, Any]) -> None:
    client.put_object(Bucket=bucket, Key=key, Body=json.dumps(report, indent=2, default=str).encode(),
                      ContentType="application/json")


__all__ = ["StateStore", "LeaseLock", "StateConflict", "LockHeld", "sync_inputs", "safe_input_name",
           "put_report", "make_s3_client", "Callable"]
