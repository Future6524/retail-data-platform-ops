"""Regression tests for the independent Project 2 review: unsafe run ids / key collisions / work-dir handling in the
scheduled task, demo workdir reuse, strict demo verification, the honest timeout message and check-runner interpreter.
All S3 interaction is the in-memory FakeS3 mock."""
import importlib.util
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest
from conftest import FAST
from fake_s3 import FakeS3

import retail_ops.demo as demo_mod
import retail_ops.task as task_mod
from retail_ops.alerts import LocalAlertManager
from retail_ops.events import read_events
from retail_ops.task import ConfigError, TaskConfig, run_task, valid_run_id

ROOT = Path(__file__).resolve().parent.parent
T0 = datetime(2026, 3, 1, 12, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def never_touch_real_aws(monkeypatch):
    monkeypatch.setattr(task_mod, "make_s3_client", lambda: (_ for _ in ()).throw(AssertionError("real boto3 client")))


class Cap:
    def __init__(self):
        self.events = []

    def __call__(self, e):
        self.events.append(e)

    def finished(self):
        return [e for e in self.events if e["event"] == "run_finished"]


def go(env, s3=None):
    s3 = s3 or FakeS3()
    cap = Cap()
    code = run_task({"RETAIL_STATE_BUCKET": "b", "RETAIL_INPUT_SOURCE": "synthetic", **env}, client=s3, emit=cap,
                    clock=lambda: T0, retry=FAST)
    return code, cap, s3


# ------------------------------------------------------------------------------------------ unsafe run ids
UNSAFE_IDS = ["../victim", "..", ".", "/abs/path", "a/b", "a\\b", "..\\victim", ".hidden", "a..b", "a\x00b", "a\nb",
              "a\x1bb", " lead", "trail ", "x" * 65, "ünï", "run:1", "-flag"]


@pytest.mark.parametrize("rid", UNSAFE_IDS, ids=[repr(r)[:20] for r in UNSAFE_IDS])
def test_unsafe_run_id_is_a_config_failure_and_nothing_outside_the_work_dir_is_touched(tmp_path, rid):
    work, victim = tmp_path / "work", tmp_path / "victim"
    victim.mkdir()
    (victim / "sentinel.txt").write_text("keep me")
    (work / "ok").mkdir(parents=True)
    (work / "ok" / "mine.txt").write_text("also keep")
    assert not valid_run_id(rid)
    with pytest.raises(ConfigError):
        TaskConfig.from_env({"RETAIL_STATE_BUCKET": "b", "RETAIL_RUN_ID": rid})
    code, cap, s3 = go({"RETAIL_WORK_DIR": str(work), "RETAIL_RUN_ID": rid})
    assert code == 2
    (fin,) = cap.finished()
    assert fin["ok"] is False and "RETAIL_RUN_ID" in fin["error"] and len(fin["error"]) < 300
    assert valid_run_id(fin["run_id"])  # the unsafe value is only quoted in the error, never used as the event's run id
    assert s3.calls == [] and s3.objects == {}  # no S3 work (no lock) before the config check
    assert (victim / "sentinel.txt").read_text() == "keep me"
    assert (work / "ok" / "mine.txt").read_text() == "also keep" and sorted(p.name for p in work.iterdir()) == ["ok"]


@pytest.mark.parametrize("rid", ["a", "run-1", "2026-03-01T12.00.00Z-ab12", "A_b.c-9", "x" * 64])
def test_safe_run_ids_work(tmp_path, rid):
    code, cap, s3 = go({"RETAIL_WORK_DIR": str(tmp_path / "w"), "RETAIL_RUN_ID": rid})
    assert code == 0 and f"reports/{rid}.json" in s3.objects and cap.finished()[0]["run_id"] == rid


# ------------------------------------------------------------------------------------------ key collisions
COLLISIONS = {
    "state==lock": {"RETAIL_STATE_KEY": "x/k", "RETAIL_LOCK_KEY": "x/k"},
    "state under reports": {"RETAIL_STATE_KEY": "reports/warehouse.duckdb"},
    "lock under reports": {"RETAIL_LOCK_KEY": "reports/pipeline.lock"},
    "state under input": {"RETAIL_STATE_KEY": "input/warehouse.duckdb"},
    "lock under input": {"RETAIL_LOCK_KEY": "input/pipeline.lock"},
    "state under custom report prefix": {"RETAIL_REPORT_PREFIX": "state/", },
    "reports nested in input": {"RETAIL_REPORT_PREFIX": "input/reports/"},
    "input nested in reports": {"RETAIL_INPUT_PREFIX": "reports/in/"},
    "same prefix": {"RETAIL_REPORT_PREFIX": "input/"},
    "report prefix without slash": {"RETAIL_REPORT_PREFIX": "reports"},
    "state is a prefix": {"RETAIL_STATE_KEY": "state/"},
    "lock is a prefix": {"RETAIL_LOCK_KEY": "locks/"},
    "control char in key": {"RETAIL_STATE_KEY": "state/a\nb"},
    "overlong key": {"RETAIL_STATE_KEY": "s/" + "k" * 1100},
    "backslash": {"RETAIL_LOCK_KEY": "locks\\x"},
}


@pytest.mark.parametrize("extra", COLLISIONS.values(), ids=COLLISIONS.keys())
def test_colliding_or_overlapping_keys_fail_config_without_touching_s3(tmp_path, extra):
    s3 = FakeS3()
    etag = s3.seed("state/warehouse.duckdb", b"precious state")
    s3.seed("input/orders_batch1.csv", b"precious input")
    with pytest.raises(ConfigError):
        TaskConfig.from_env({"RETAIL_STATE_BUCKET": "b", **extra})
    code, cap, _ = go({"RETAIL_WORK_DIR": str(tmp_path / "w"), **extra}, s3)
    assert code == 2 and cap.finished()[0]["ok"] is False
    assert s3.calls == [] and s3.objects["state/warehouse.duckdb"] == (b"precious state", etag)
    assert s3.objects["input/orders_batch1.csv"][0] == b"precious input"
    assert not (tmp_path / "w").exists()


def test_distinct_non_overlapping_custom_keys_are_accepted():
    cfg = TaskConfig.from_env({"RETAIL_STATE_BUCKET": "b", "RETAIL_STATE_KEY": "s/db", "RETAIL_LOCK_KEY": "l/lock",
                               "RETAIL_INPUT_PREFIX": "in/", "RETAIL_REPORT_PREFIX": "rep/"})
    assert (cfg.state_key, cfg.lock_key, cfg.input_prefix, cfg.report_prefix) == ("s/db", "l/lock", "in/", "rep/")


@pytest.mark.parametrize("wd", ["/", ""])
def test_work_dir_must_be_dedicated(wd):
    with pytest.raises(ConfigError, match="RETAIL_WORK_DIR"):
        TaskConfig.from_env({"RETAIL_STATE_BUCKET": "b", "RETAIL_WORK_DIR": wd})


# ------------------------------------------------------------------------------------------ own work-dir cleanup
def test_existing_own_work_entry_is_replaced_and_cleaned_siblings_untouched(tmp_path):
    work = tmp_path / "work"
    (work / "r1" / "input").mkdir(parents=True)
    (work / "r1" / "stale.txt").write_text("old run leftovers")
    (work / "other-run").mkdir()
    (work / "other-run" / "f").write_text("sibling")
    code, _, _ = go({"RETAIL_WORK_DIR": str(work), "RETAIL_RUN_ID": "r1"})
    assert code == 0 and sorted(p.name for p in work.iterdir()) == ["other-run"]
    assert (work / "other-run" / "f").read_text() == "sibling"


def test_existing_regular_file_at_the_work_path_is_replaced(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    (work / "r1").write_text("a file squatting on the run's directory name")
    code, _, _ = go({"RETAIL_WORK_DIR": str(work), "RETAIL_RUN_ID": "r1"})
    assert code == 0 and list(work.iterdir()) == []


def test_symlink_at_the_work_path_is_unlinked_never_followed(tmp_path):
    work, victim = tmp_path / "work", tmp_path / "victim"
    work.mkdir()
    victim.mkdir()
    (victim / "sentinel.txt").write_text("keep me")
    (work / "r1").symlink_to(victim, target_is_directory=True)
    code, _, _ = go({"RETAIL_WORK_DIR": str(work), "RETAIL_RUN_ID": "r1"})
    assert code == 0
    assert (victim / "sentinel.txt").read_text() == "keep me" and [p.name for p in victim.iterdir()] == ["sentinel.txt"]
    assert list(work.iterdir()) == []


def test_dangling_symlink_at_the_work_path_is_replaced(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    (work / "r1").symlink_to(tmp_path / "nowhere")
    code, _, _ = go({"RETAIL_WORK_DIR": str(work), "RETAIL_RUN_ID": "r1"})
    assert code == 0 and list(work.iterdir()) == []


def test_work_dir_is_created_and_cleaned_even_when_the_run_crashes(tmp_path, monkeypatch):
    monkeypatch.setattr(task_mod, "sync_inputs", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("s3 down")))
    work = tmp_path / "newdir" / "work"
    code, _, s3 = go({"RETAIL_WORK_DIR": str(work), "RETAIL_INPUT_SOURCE": "s3", "RETAIL_RUN_ID": "r1"})
    assert code == 4 and work.is_dir() and list(work.iterdir()) == [] and "locks/pipeline.lock" not in s3.objects


# ------------------------------------------------------------------------------------------ watchdog honesty
def test_watchdog_reports_unknown_commit_outcome_not_a_false_not_committed_claim(monkeypatch):
    fired = {}

    class FakeTimer:
        def __init__(self, seconds, fn):
            fired["fn"], fired["seconds"] = fn, seconds
            self.daemon = False

        def start(self):
            pass

    monkeypatch.setattr(task_mod.threading, "Timer", FakeTimer)
    monkeypatch.setattr(task_mod.os, "_exit", lambda code: fired.setdefault("exit", code))
    cap = Cap()
    task_mod._watchdog({"RETAIL_TIMEOUT_SECONDS": "45", "RETAIL_RUN_ID": "../evil"}, cap, lambda: T0)
    fired["fn"]()
    (fin,) = cap.finished()
    assert fired["exit"] == 5 and fired["seconds"] == 45 and valid_run_id(fin["run_id"])
    msg = fin["error"].lower()
    assert "unknown" in msg and "possibly committed" in msg and "checkpoint" in msg
    assert "not committed" not in msg
    assert "without committing" not in (task_mod.__doc__ or "").lower() and "unknown" in (task_mod.__doc__ or "").lower()


# ------------------------------------------------------------------------------------------ demo workdir reuse
def test_prepare_workdir_accepts_an_existing_empty_directory(tmp_path):
    d = tmp_path / "empty"
    d.mkdir()
    demo_mod.prepare_workdir(d)
    assert (d / demo_mod.MARKER).is_file()


def test_prepare_workdir_refuses_foreign_content_files_and_symlinks_to_them(tmp_path):
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    (foreign / "keep.txt").write_text("mine")
    link = tmp_path / "link"
    link.symlink_to(foreign, target_is_directory=True)
    afile = tmp_path / "afile"
    afile.write_text("x")
    for target in (foreign, link, afile):
        with pytest.raises(SystemExit, match="refusing"):
            demo_mod.prepare_workdir(target)
    assert (foreign / "keep.txt").read_text() == "mine" and afile.read_text() == "x" and link.is_symlink()


def test_demo_runs_in_an_existing_empty_directory(tmp_path, capsys):
    d = tmp_path / "pre-made"
    d.mkdir()
    assert demo_mod.run_demo(d) == 0
    assert "demo invariants held" in capsys.readouterr().out


# ------------------------------------------------------------------------------------------ strict demo verification
@pytest.fixture(scope="module")
def good_demo(tmp_path_factory):
    d = tmp_path_factory.mktemp("good-demo")
    res = demo_mod.execute_demo(d / "w", say=lambda *_: None)
    assert res.ok, res.problems
    return res


def test_demo_verification_requires_real_fired_and_resolved_transitions(good_demo):
    assert [(a["event"], a["run_id"]) for a in good_demo.alert_events] == [("alert_fired", "run-003"),
                                                                           ("alert_resolved", "run-004")]
    assert demo_mod.verify_demo(good_demo.events, good_demo.alert_events, False) == []
    # "no active alert" alone must NOT be enough: with the alert events missing, the verifier objects
    assert demo_mod.verify_demo(good_demo.events, [], False)
    assert demo_mod.verify_demo(good_demo.events, good_demo.alert_events[:1], False)  # fired but never resolved
    assert demo_mod.verify_demo(good_demo.events, good_demo.alert_events, True)  # leave_firing expects it unresolved


def _mutate(events, rid, **fields):
    out = []
    for e in events:
        out.append({**e, **fields} if e["event"] == "run_finished" and e["run_id"] == rid else e)
    return out


@pytest.mark.parametrize("rid,fields", [
    ("run-001", {"ok": False}), ("run-001", {"files": {}}), ("run-002", {"files": {"failed": 1, "loaded": 2}}),
    ("run-003", {"ok": True}), ("run-003", {"synthetic_fault": None}), ("run-004", {"ok": False}),
    ("run-004", {"files": {"skipped_checkpointed": 10}})])
def test_demo_verification_rejects_wrong_run_outcomes(good_demo, rid, fields):
    assert demo_mod.verify_demo(_mutate(good_demo.events, rid, **fields), good_demo.alert_events, False)


def test_demo_fails_when_alerts_are_suppressed(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(LocalAlertManager, "evaluate", lambda self, finished: None)
    assert demo_mod.run_demo(tmp_path / "w") == 1
    out = capsys.readouterr().out
    assert "demo invariants FAILED" in out and "alert transitions" in out and "invariants held" not in out
    assert read_events(tmp_path / "w" / "logs" / "alerts.jsonl") == []


def test_demo_fails_when_only_the_resolution_is_suppressed(tmp_path, monkeypatch, capsys):
    real = LocalAlertManager.evaluate

    def only_fire(self, finished):
        return real(self, finished) if not finished.get("ok") else None

    monkeypatch.setattr(LocalAlertManager, "evaluate", only_fire)
    assert demo_mod.run_demo(tmp_path / "w") == 1
    assert "FAILED" in capsys.readouterr().out


def test_demo_fails_when_the_fault_is_not_actually_injected(tmp_path, monkeypatch, capsys):
    class NoFault:
        def __init__(self, *a, **k):
            pass

        def install(self):
            return self

        def remove(self):
            pass

    monkeypatch.setattr(demo_mod, "FaultPlan", NoFault)
    assert demo_mod.run_demo(tmp_path / "w") == 1  # run-003 succeeds -> no alert -> verifier objects
    assert "FAILED" in capsys.readouterr().out


# ------------------------------------------------------------------------------------------ check runner interpreter
def _load_run_checks():
    spec = importlib.util.spec_from_file_location("run_checks_under_test", ROOT / "scripts" / "run_checks.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_run_checks_uses_the_current_interpreter_even_if_a_repo_venv_exists(monkeypatch):
    monkeypatch.setattr(sys, "executable", "/fresh/review-venv/bin/python")
    rc = _load_run_checks()
    assert rc.PY == "/fresh/review-venv/bin/python"
    python_steps = {name: cmd for name, cmd, *_ in rc.steps() if name in ("ruff", "mypy", "pytest", "hygiene", "ops-demo")}
    assert set(python_steps) == {"ruff", "mypy", "pytest", "hygiene", "ops-demo"}
    assert all(cmd[0] == "/fresh/review-venv/bin/python" for cmd in python_steps.values())
    assert str(ROOT / ".venv") not in " ".join(" ".join(c) for c in python_steps.values())


def test_run_checks_source_has_no_repo_venv_preference():
    src = (ROOT / "scripts" / "run_checks.py").read_text()
    assert '".venv"' not in src and "sys.executable" in src


def test_run_checks_runs_a_subprocess_with_the_launching_interpreter(tmp_path):
    import subprocess
    code = ("import sys, importlib.util as u; s=u.spec_from_file_location('rc', sys.argv[1]); m=u.module_from_spec(s); "
            "s.loader.exec_module(m); print(m.PY == sys.executable and m.PY)")
    out = subprocess.run([sys.executable, "-I", "-c", code, str(ROOT / "scripts" / "run_checks.py")],  # noqa: S603
                         capture_output=True, text=True, check=True, cwd=tmp_path).stdout.strip()
    assert out == sys.executable and os.path.isabs(out)
