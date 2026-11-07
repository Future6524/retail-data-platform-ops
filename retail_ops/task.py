"""ECS/Fargate scheduled-task entrypoint: S3 state -> run pipeline -> conditional S3 commit.

Environment (set by the Terraform task definition; no secrets are required for the default DuckDB backend):
  RETAIL_STATE_BUCKET   required. Versioned, private bucket.
  RETAIL_STATE_KEY      default state/warehouse.duckdb
  RETAIL_LOCK_KEY       default locks/pipeline.lock
  RETAIL_INPUT_PREFIX   default input/        (source batch files are READ from here, never modified)
  RETAIL_REPORT_PREFIX  default reports/
  RETAIL_WORK_DIR       default /work         (writable scratch; the container root FS is read-only)
  RETAIL_INPUT_SOURCE   s3 (default) | synthetic (generate the demo batches locally; for smoke runs)
  RETAIL_TIMEOUT_SECONDS default 3600 (watchdog: hard-exits 5; see "Timeout semantics"), must be < lock TTL
  RETAIL_LOCK_TTL_SECONDS default 7200, RETAIL_MAX_ATTEMPTS default 3
  RETAIL_RUN_ID optional; must match [A-Za-z0-9][A-Za-z0-9._-]{0,63} with no '..' (it names a local directory,
  an S3 report key and the lock owner), otherwise exit 2 before any filesystem or S3 work.
Timeout semantics: the watchdog cannot know whether the single conditional state PUT had already completed when it
fired. The exit-5 event therefore says the commit outcome is UNKNOWN (possibly committed). No atomicity is claimed:
the next run downloads whatever state exists and its checkpoints decide what is skipped vs. redone (idempotent). The
lease lock is not released by a hard exit; it expires after RETAIL_LOCK_TTL_SECONDS.
Exit codes: 0 ok | 1 some file failed | 2 configuration error | 3 concurrency (lock held / state changed) | 4 crash | 5 timeout
Every outcome emits JSON events on stdout; failures emit run_finished with ok=false (CloudWatch metric filter input).
"""
from __future__ import annotations

import os
import re
import shutil
import sys
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from pipeline.retry import RetryPolicy
from pipeline.warehouse import open_warehouse

from .events import Clock, Emitter, make_event, stdout_emitter, utc_now
from .instrument import execute_run
from .s3state import LeaseLock, LockHeld, StateConflict, StateStore, make_s3_client, put_report, sync_inputs

KEY_RE_FORBIDDEN = ("..", "\\")
RUN_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
MAX_KEY_BYTES = 1024


class ConfigError(ValueError):
    pass


def valid_run_id(run_id: str) -> bool:
    """Safe as a single path component, an S3 key segment and a lock owner (no separators, dots-only, control chars)."""
    return RUN_ID_RE.fullmatch(run_id) is not None and ".." not in run_id


def _overlaps(a: str, b: str) -> bool:
    return a.startswith(b) or b.startswith(a)


@dataclass(frozen=True)
class TaskConfig:
    bucket: str
    state_key: str = "state/warehouse.duckdb"
    lock_key: str = "locks/pipeline.lock"
    input_prefix: str = "input/"
    report_prefix: str = "reports/"
    work_dir: Path = Path("/work")
    input_source: str = "s3"
    lock_ttl: int = 7200
    timeout: int = 3600
    max_attempts: int = 3
    run_id: str = ""

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> "TaskConfig":
        bucket = env.get("RETAIL_STATE_BUCKET", "").strip()
        if not bucket:
            raise ConfigError("RETAIL_STATE_BUCKET is required")

        def key(name: str, default: str) -> str:
            v = env.get(name, default)
            if (not v or v.startswith("/") or any(bad in v for bad in KEY_RE_FORBIDDEN) or len(v.encode()) > MAX_KEY_BYTES
                    or any(ord(ch) < 32 or ord(ch) == 127 for ch in v)):
                raise ConfigError(f"{name} is not a safe S3 key/prefix: {v!r}")
            return v

        run_id = env.get("RETAIL_RUN_ID", "") or ""
        if run_id and not valid_run_id(run_id):
            raise ConfigError(f"RETAIL_RUN_ID is not a safe identifier (allowed: [A-Za-z0-9][A-Za-z0-9._-]{{0,63}}): {run_id[:80]!r}")

        src = env.get("RETAIL_INPUT_SOURCE", "s3")
        if src not in ("s3", "synthetic"):
            raise ConfigError("RETAIL_INPUT_SOURCE must be 's3' or 'synthetic'")
        try:
            ttl, attempts = int(env.get("RETAIL_LOCK_TTL_SECONDS", "7200")), int(env.get("RETAIL_MAX_ATTEMPTS", "3"))
            timeout = int(env.get("RETAIL_TIMEOUT_SECONDS", "3600"))
        except ValueError as exc:
            raise ConfigError("RETAIL_LOCK_TTL_SECONDS / RETAIL_MAX_ATTEMPTS / RETAIL_TIMEOUT_SECONDS must be integers") from exc
        if ttl < 60 or attempts < 1 or timeout < 30:
            raise ConfigError("lock TTL must be >= 60, attempts >= 1, timeout >= 30")
        if ttl <= timeout:
            raise ConfigError("RETAIL_LOCK_TTL_SECONDS must exceed RETAIL_TIMEOUT_SECONDS (else a live run could lose its lock)")
        in_prefix, report_prefix = key("RETAIL_INPUT_PREFIX", "input/"), key("RETAIL_REPORT_PREFIX", "reports/")
        state_key, lock_key = key("RETAIL_STATE_KEY", "state/warehouse.duckdb"), key("RETAIL_LOCK_KEY", "locks/pipeline.lock")
        for name, prefix in (("RETAIL_INPUT_PREFIX", in_prefix), ("RETAIL_REPORT_PREFIX", report_prefix)):
            if not prefix.endswith("/"):
                raise ConfigError(f"{name} must end with '/'")
        for name, k in (("RETAIL_STATE_KEY", state_key), ("RETAIL_LOCK_KEY", lock_key)):
            if k.endswith("/"):
                raise ConfigError(f"{name} must be an object key, not a prefix (no trailing '/')")
        # Misconfiguration must never let reports, the lock or the state overwrite/shadow each other or the inputs.
        if state_key == lock_key:
            raise ConfigError("RETAIL_STATE_KEY and RETAIL_LOCK_KEY must differ (the lock would overwrite the state)")
        if _overlaps(in_prefix, report_prefix):
            raise ConfigError("RETAIL_INPUT_PREFIX and RETAIL_REPORT_PREFIX must not overlap (reports would be read as inputs)")
        for name, k in (("RETAIL_STATE_KEY", state_key), ("RETAIL_LOCK_KEY", lock_key)):
            for pname, prefix in (("RETAIL_INPUT_PREFIX", in_prefix), ("RETAIL_REPORT_PREFIX", report_prefix)):
                if k.startswith(prefix):
                    raise ConfigError(f"{name} must not live under {pname} ({prefix!r})")
        work_dir = Path(env.get("RETAIL_WORK_DIR", "/work"))
        if not str(env.get("RETAIL_WORK_DIR", "/work")).strip() or work_dir.resolve() == Path(work_dir.resolve().anchor):
            raise ConfigError("RETAIL_WORK_DIR must be a dedicated directory, not empty or the filesystem root")
        return cls(bucket=bucket, state_key=state_key, lock_key=lock_key, input_prefix=in_prefix,
                   report_prefix=report_prefix, work_dir=work_dir, input_source=src, lock_ttl=ttl, timeout=timeout,
                   max_attempts=attempts, run_id=run_id)


def _fail(emit: Emitter, run_id: str, clock: Clock, error: str, code: int) -> int:
    emit(make_event("run_finished", run_id, clock, ok=False, outcome="failure", duration_seconds=0.0, files={}, totals={}, error=error))
    return code


def run_task(env: Mapping[str, str], client: Any | None = None, emit: Emitter | None = None,
             clock: Clock = utc_now, retry: RetryPolicy | None = None) -> int:
    emit = emit or stdout_emitter()
    requested = env.get("RETAIL_RUN_ID") or ""
    generated = f"{clock().strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"
    run_id = requested if valid_run_id(requested) else generated  # an unsafe id is never used, only reported
    try:
        cfg = TaskConfig.from_env(env)
    except ConfigError as exc:
        return _fail(emit, run_id, clock, f"ConfigError: {exc}", 2)
    try:
        client = client or make_s3_client()
    except Exception as exc:  # noqa: BLE001
        return _fail(emit, run_id, clock, f"cannot create S3 client: {type(exc).__name__}", 4)

    lock = LeaseLock(client, cfg.bucket, cfg.lock_key, owner=run_id, ttl_seconds=cfg.lock_ttl, clock=clock)
    try:
        lock.acquire()
    except LockHeld as exc:
        return _fail(emit, run_id, clock, f"LockHeld: {exc}", 3)
    except Exception as exc:  # noqa: BLE001
        return _fail(emit, run_id, clock, f"lock error: {type(exc).__name__}: {exc}", 4)
    try:
        return _locked_run(cfg, client, emit, clock, run_id, retry)
    except StateConflict as exc:
        return _fail(emit, run_id, clock, f"StateConflict: {exc}", 3)
    except Exception as exc:  # noqa: BLE001
        return _fail(emit, run_id, clock, f"crash: {type(exc).__name__}: {exc}"[:500], 4)
    finally:
        lock.release()


def _locked_run(cfg: TaskConfig, client: Any, emit: Emitter, clock: Clock, run_id: str,
                retry: RetryPolicy | None) -> int:
    work = _fresh_work_dir(cfg.work_dir, run_id)
    try:
        return _run_in(work, cfg, client, emit, clock, run_id, retry)
    finally:
        _remove_owned(work)


def _remove_owned(path: Path) -> None:
    """Remove ``path`` (a direct child of the work dir that this task owns). Never follows a symlink."""
    if path.is_symlink() or path.is_file():
        path.unlink(missing_ok=True)
    elif path.exists():
        shutil.rmtree(path, ignore_errors=True)


def _fresh_work_dir(base: Path, run_id: str) -> Path:
    """Create an empty ``base/run_id``. A leftover entry of that exact name (file, symlink, dir) is replaced; a
    symlink itself is removed, never its target. Raises ConfigError if the result would not be inside ``base``."""
    if not valid_run_id(run_id):  # defence in depth: from_env already rejected this
        raise ConfigError(f"unsafe run id {run_id!r}")
    base.mkdir(parents=True, exist_ok=True)
    root = base.resolve()
    work = root / run_id
    _remove_owned(work)
    work.mkdir()
    if work.is_symlink() or work.resolve().parent != root:
        raise ConfigError("work directory escapes RETAIL_WORK_DIR")
    return work


def _run_in(work: Path, cfg: TaskConfig, client: Any, emit: Emitter, clock: Clock, run_id: str,
            retry: RetryPolicy | None) -> int:
    inputs, db = work / "input", work / "warehouse.duckdb"
    store = StateStore(client, cfg.bucket, cfg.state_key)
    etag = store.download(db)
    emit(make_event("run_started", run_id, clock, trigger="schedule", state_etag=etag, state_existed=etag is not None))
    if cfg.input_source == "synthetic":
        from data import generate_demo_batches
        generate_demo_batches(inputs, seed=42)
        skipped: list[str] = []
    else:
        sync = sync_inputs(client, cfg.bucket, cfg.input_prefix, inputs)
        skipped = sync.skipped
    wh = open_warehouse("duckdb", db)
    held: list[dict[str, Any]] = []  # run_started is emitted above; run_finished only AFTER the state commit

    def forward(e: dict[str, Any]) -> None:
        if e["event"] == "run_finished":
            held.append(e)
        elif e["event"] != "run_started":
            emit(e)

    try:
        outcome = execute_run(wh, inputs, run_id=run_id, emit=forward, trigger="schedule", clock=clock,
                              retry=retry or RetryPolicy(max_attempts=cfg.max_attempts))
    finally:
        wh.close()  # flushes the DuckDB WAL into the single file we upload
    new_etag = None
    if outcome.changed_state:
        new_etag = store.upload(db, etag)  # StateConflict propagates: partial work is NOT published
    report: dict[str, Any] = {"run_id": run_id, "ok": outcome.ok, "state_committed": new_etag is not None,
                              "skipped_keys": skipped, "report": outcome.report.as_dict() if outcome.report else None,
                              "error": outcome.error}
    put_report(client, cfg.bucket, f"{cfg.report_prefix}{run_id}.json", report)
    emit({**held[0], "state_committed": new_etag is not None, "state_etag": new_etag})
    return 0 if outcome.ok else 1


def _watchdog(env: Mapping[str, str], emit: Emitter, clock: Clock) -> threading.Timer | None:
    try:
        seconds = int(env.get("RETAIL_TIMEOUT_SECONDS", "3600"))
    except ValueError:
        return None  # run_task reports the configuration error

    def expire() -> None:
        rid = env.get("RETAIL_RUN_ID") or ""
        _fail(emit, rid if valid_run_id(rid) else "timeout", clock,
              f"timeout after {seconds}s; state commit outcome UNKNOWN (possibly committed if the upload had already "
              "finished; the next run's checkpoints settle it)", 5)
        os._exit(5)  # hard exit; no atomicity claim: the next run re-reads the state and its checkpoints decide

    t = threading.Timer(seconds, expire)
    t.daemon = True
    t.start()
    return t


def main(argv: list[str] | None = None) -> int:
    emit = stdout_emitter()
    timer = _watchdog(os.environ, emit, utc_now)
    try:
        return run_task(os.environ, emit=emit)
    finally:
        if timer:
            timer.cancel()


if __name__ == "__main__":
    sys.exit(main())
