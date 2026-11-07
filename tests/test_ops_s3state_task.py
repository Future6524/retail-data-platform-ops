"""S3 checkpoint, lease lock and the scheduled-task entrypoint. ALL S3 interaction is an in-memory MOCK (FakeS3):
these tests show the logic is sound against modelled conditional-write semantics, not that AWS was exercised."""
import json
from datetime import datetime, timedelta, timezone

import pytest
from conftest import FAST
from fake_s3 import FakeS3

import retail_ops.task as task_mod
from data import generate_demo_batches
from pipeline.faults import FaultPlan
from pipeline.warehouse import open_warehouse
from retail_ops.events import Emitter, validate_event
from retail_ops.s3state import LeaseLock, LockHeld, StateConflict, StateStore, safe_input_name, sync_inputs
from retail_ops.task import ConfigError, TaskConfig, run_task

T0 = datetime(2026, 3, 1, 12, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def never_touch_real_aws(monkeypatch):
    def boom():
        raise AssertionError("tests must never create a real boto3 client")
    monkeypatch.setattr(task_mod, "make_s3_client", boom)


# ------------------------------------------------------------------------------------------ StateStore
def test_state_first_upload_requires_absence_then_if_match(tmp_path):
    s3 = FakeS3()
    store = StateStore(s3, "b", "state/w.duckdb")
    f = tmp_path / "w.duckdb"
    f.write_bytes(b"v1")
    assert store.download(tmp_path / "dl") is None
    e1 = store.upload(f, None)
    with pytest.raises(StateConflict):  # someone else already created it: If-None-Match:* must refuse
        store.upload(f, None)
    f.write_bytes(b"v2")
    e2 = store.upload(f, e1)
    with pytest.raises(StateConflict):  # stale ETag
        store.upload(f, e1)
    assert s3.objects["state/w.duckdb"][0] == b"v2" and e2 != e1
    assert store.download(tmp_path / "dl") == e2 and (tmp_path / "dl").read_bytes() == b"v2"


def test_non_conflict_errors_propagate(tmp_path):
    class Broken(FakeS3):
        def put_object(self, **kw):
            from fake_s3 import FakeClientError
            raise FakeClientError("AccessDenied")
    f = tmp_path / "x"
    f.write_bytes(b"1")
    with pytest.raises(Exception, match="AccessDenied"):
        StateStore(Broken(), "b", "k").upload(f, None)


# ------------------------------------------------------------------------------------------ LeaseLock
def test_lock_exclusion_expiry_takeover_and_safe_release():
    s3 = FakeS3()
    now = [T0]
    mk = lambda owner, ttl=600: LeaseLock(s3, "b", "locks/l", owner, ttl, clock=lambda: now[0])
    a, b = mk("A"), mk("B")
    a.acquire()
    with pytest.raises(LockHeld, match="owner='A'"):
        b.acquire()
    now[0] = T0 + timedelta(seconds=601)  # A's lease expired (crashed task): B may take over
    b.acquire()
    a.release()  # A wakes up late: must NOT delete B's lock
    assert "locks/l" in s3.objects
    with pytest.raises(LockHeld):
        mk("C").acquire()
    b.release()
    assert "locks/l" not in s3.objects
    b.release()  # idempotent


def test_lock_with_unreadable_body_fails_safe():
    s3 = FakeS3()
    s3.seed("locks/l", b"not json{")
    with pytest.raises(Exception):
        LeaseLock(s3, "b", "locks/l", "A", clock=lambda: T0).acquire()
    s3.seed("locks/l", b'{"owner":"x"}')  # no expiry field: do not guess
    with pytest.raises(LockHeld):
        LeaseLock(s3, "b", "locks/l", "A", clock=lambda: T0).acquire()


# ------------------------------------------------------------------------------------------ input handling
@pytest.mark.parametrize("key,expect", [
    ("input/orders_1.csv", "orders_1.csv"), ("input/Inventory-2.JSONL", "Inventory-2.JSONL"),
    ("input/../state/warehouse.duckdb", None), ("input/sub/orders.csv", None), ("input/..", None),
    ("input/", None), ("input/.hidden.csv", None), ("input/a b.csv", None), ("input/orders.exe", None),
    ("input/orders.csv\x00.exe", None), ("other/orders.csv", None), ("input/..orders.csv", None),
    ("input/" + "a" * 200 + ".csv", None), ("input/orders_1.csv/", None),
])
def test_safe_input_name(key, expect):
    assert safe_input_name(key, "input/") == expect


def test_sync_inputs_paginates_skips_unsafe_and_never_escapes(tmp_path):
    s3 = FakeS3(page_size=2)
    for i in range(5):
        s3.seed(f"input/orders_{i}.csv", b"x")
    s3.seed("input/../evil.csv", b"x")
    s3.seed("input/sub/deep.csv", b"x")
    s3.seed("input/notes.txt", b"x")
    s3.seed("state/warehouse.duckdb", b"secret-state")
    res = sync_inputs(s3, "b", "input/", tmp_path / "in")
    assert res.downloaded == [f"orders_{i}.csv" for i in range(5)]
    assert sorted(res.skipped) == ["input/../evil.csv", "input/notes.txt", "input/sub/deep.csv"]
    assert sorted(p.name for p in (tmp_path / "in").iterdir()) == res.downloaded
    assert not (tmp_path / "evil.csv").exists()
    with pytest.raises(RuntimeError, match="more than 3"):
        sync_inputs(s3, "b", "input/", tmp_path / "in2", max_files=3)


# ------------------------------------------------------------------------------------------ config
def test_config_validation(tmp_path):
    ok = {"RETAIL_STATE_BUCKET": "b"}
    assert TaskConfig.from_env(ok).state_key == "state/warehouse.duckdb"
    for bad in ({}, {"RETAIL_STATE_BUCKET": " "}, {**ok, "RETAIL_STATE_KEY": "../x"}, {**ok, "RETAIL_STATE_KEY": "/abs"},
                {**ok, "RETAIL_INPUT_PREFIX": "input"}, {**ok, "RETAIL_INPUT_SOURCE": "ftp"},
                {**ok, "RETAIL_LOCK_TTL_SECONDS": "x"}, {**ok, "RETAIL_MAX_ATTEMPTS": "0"},
                {**ok, "RETAIL_TIMEOUT_SECONDS": "7200", "RETAIL_LOCK_TTL_SECONDS": "7200"}):
        with pytest.raises(ConfigError):
            TaskConfig.from_env(bad)


# ------------------------------------------------------------------------------------------ the task
class Capture:
    def __init__(self):
        self.events = []

    def __call__(self, e):
        self.events.append(e)

    def finished(self):
        return [e for e in self.events if e["event"] == "run_finished"]


def run(s3, work, source="synthetic", cap=None, rid="run-x", **extra):
    env = {"RETAIL_STATE_BUCKET": "bkt", "RETAIL_WORK_DIR": str(work), "RETAIL_INPUT_SOURCE": source,
           "RETAIL_RUN_ID": rid, **extra}
    cap = cap or Capture()
    code = run_task(env, client=s3, emit=cap, clock=lambda: T0, retry=FAST)
    return code, cap


def test_first_run_commits_state_report_and_releases_lock(tmp_path):
    s3 = FakeS3()
    code, cap = run(s3, tmp_path)
    assert code == 0
    assert set(s3.objects) == {"state/warehouse.duckdb", "reports/run-x.json"}  # lock released, state + report written
    assert ("put", "state/warehouse.duckdb") in s3.calls
    fin = cap.finished()
    assert len(fin) == 1 and fin[0]["ok"] and fin[0]["outcome"] == "success" and fin[0]["state_committed"] is True
    assert all(validate_event(e) == [] for e in cap.events)
    report = json.loads(s3.objects["reports/run-x.json"][0])
    assert report["ok"] and report["state_committed"]
    assert not list(tmp_path.iterdir())  # scratch dir cleaned up


def test_state_persists_across_ephemeral_runs_and_noop_run_does_not_rewrite(tmp_path):
    s3 = FakeS3()
    assert run(s3, tmp_path / "a", rid="r1")[0] == 0
    etag1 = s3.objects["state/warehouse.duckdb"][1]
    # "new task, empty disk": the 2nd run must restore the checkpoint from S3 and skip everything
    code, cap = run(s3, tmp_path / "b", rid="r2")
    assert code == 0
    statuses = {e["status"] for e in cap.events if e["event"] == "file_result"}
    assert statuses == {"skipped_checkpointed"}
    assert cap.finished()[0]["state_committed"] is False
    assert s3.objects["state/warehouse.duckdb"][1] == etag1  # nothing changed, no new version written
    # and the persisted file really is a usable warehouse with the loaded data
    p = tmp_path / "check.duckdb"
    p.write_bytes(s3.objects["state/warehouse.duckdb"][0])
    wh = open_warehouse("duckdb", p)
    assert wh.table_counts()["orders"] > 0
    wh.close()


def test_s3_input_source_loads_only_safe_keys(tmp_path):
    s3 = FakeS3()
    gen = tmp_path / "gen"
    generate_demo_batches(gen, seed=42)
    for f in gen.iterdir():
        if f.name != "manifest.json":
            s3.seed(f"input/{f.name}", f.read_bytes())
    s3.seed("input/../evil.csv", b"x")
    code, cap = run(s3, tmp_path / "w", source="s3")
    assert code == 0
    loaded = [e["file"] for e in cap.events if e["event"] == "file_result" and e["status"] == "loaded"]
    assert loaded and "evil.csv" not in loaded
    assert json.loads(s3.objects["reports/run-x.json"][0])["skipped_keys"] == ["input/../evil.csv"]


def test_concurrent_run_is_refused_by_lock(tmp_path):
    s3 = FakeS3()
    s3.seed("locks/pipeline.lock", json.dumps({"owner": "other", "expires": (T0 + timedelta(hours=1)).isoformat()}).encode())
    code, cap = run(s3, tmp_path)
    assert code == 3
    fin = cap.finished()
    assert len(fin) == 1 and fin[0]["outcome"] == "failure" and "LockHeld" in fin[0]["error"]
    assert "state/warehouse.duckdb" not in s3.objects
    assert "locks/pipeline.lock" in s3.objects  # we did not delete someone else's lock


def test_state_changed_by_someone_else_is_never_overwritten(tmp_path):
    s3 = FakeS3()
    assert run(s3, tmp_path / "a", rid="r1", RETAIL_INPUT_SOURCE="synthetic")[0] == 0
    # second run sees new input (an extra batch file), but a rogue writer replaces the state mid-run
    s3b = s3
    s3b.objects.pop("reports/r1.json")
    gen = tmp_path / "gen"
    generate_demo_batches(gen, seed=7)
    for f in gen.iterdir():
        if f.name != "manifest.json":
            s3b.seed(f"input/{f.name}", f.read_bytes())

    def intruder(key):
        if key == "state/warehouse.duckdb":
            s3b.objects[key] = (b"WRITTEN-BY-ANOTHER-WRITER", '"intruder"')
    s3b.before_put = intruder
    code, cap = run(s3b, tmp_path / "b", source="s3", rid="r2")
    assert code == 3
    assert s3b.objects["state/warehouse.duckdb"][0] == b"WRITTEN-BY-ANOTHER-WRITER"  # NOT clobbered
    fin = cap.finished()
    assert len(fin) == 1 and fin[0]["outcome"] == "failure" and "StateConflict" in fin[0]["error"]
    assert "locks/pipeline.lock" not in s3b.objects  # lock released even on conflict


def test_file_failure_exits_1_alerts_but_still_commits_good_files(tmp_path, monkeypatch):
    s3 = FakeS3()
    real_open = task_mod.open_warehouse

    def faulty(backend, path):
        wh = real_open(backend, path)
        FaultPlan(wh, {"orders_batch3.jsonl": 99}).install()
        return wh
    monkeypatch.setattr(task_mod, "open_warehouse", faulty)
    code, cap = run(s3, tmp_path)
    assert code == 1
    fin = cap.finished()
    assert len(fin) == 1 and fin[0]["ok"] is False and fin[0]["outcome"] == "failure"
    assert fin[0]["state_committed"] is True and "orders_batch3.jsonl" in fin[0]["error"]
    assert "state/warehouse.duckdb" in s3.objects  # partial progress is kept; the failed file will retry next run
    monkeypatch.setattr(task_mod, "open_warehouse", real_open)
    code2, cap2 = run(s3, tmp_path, rid="run-y")
    assert code2 == 0
    loaded = [e["file"] for e in cap2.events if e["event"] == "file_result" and e["status"] == "loaded"]
    assert loaded == ["orders_batch3.jsonl"]  # exactly the previously failed file


def test_config_errors_exit_2_and_emit_a_failure_event(tmp_path):
    cap = Capture()
    code = run_task({}, client=FakeS3(), emit=cap, clock=lambda: T0)
    assert code == 2 and cap.finished()[0]["outcome"] == "failure" and "RETAIL_STATE_BUCKET" in cap.finished()[0]["error"]
    assert validate_event(cap.finished()[0]) == []


def test_crash_exits_4_and_releases_lock(tmp_path, monkeypatch):
    s3 = FakeS3()
    monkeypatch.setattr(task_mod, "sync_inputs", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("s3 down")))
    code, cap = run(s3, tmp_path, source="s3")
    assert code == 4 and "s3 down" in cap.finished()[0]["error"] and "locks/pipeline.lock" not in s3.objects


def test_stdout_emitter_is_json_lines(capsys):
    import sys
    cap = Emitter(stream=sys.stdout)
    run_task({}, client=FakeS3(), emit=cap, clock=lambda: T0)
    line = capsys.readouterr().out.strip()
    assert json.loads(line)["event"] == "run_finished"
