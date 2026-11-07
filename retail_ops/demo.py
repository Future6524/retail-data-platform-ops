"""One-command LOCAL monitoring demo (no Docker, no network, no external alerts).

    python -m retail_ops demo [--workdir .demo/monitoring] [--leave-firing]

Executes the REAL pipeline four times against SYNTHETIC data and records structured logs/metrics/alerts:
  1. run-001  success (batch 1)            2. run-002  success (batches 1-2)
  3. run-003  FAILURE: a SYNTHETIC injected commit fault (not a real outage) -> local alert FIRES
  4. run-004  recovery run without the fault -> alert RESOLVES   (skipped with --leave-firing)
"""
from __future__ import annotations

import logging
import shutil
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from data import generate_demo_batches
from pipeline.faults import FaultPlan
from pipeline.retry import RetryPolicy
from pipeline.runner import FileReport
from pipeline.warehouse import open_warehouse

from .alerts import LocalAlertManager, firing_alerts
from .dashboard import RUN_LOG, load_events, render_html
from .events import Emitter, Event, read_events, validate_event
from .instrument import RunOutcome, execute_run
from .metrics import render_metrics

MARKER = ".retail-ops-demo"
AS_OF = datetime(2026, 2, 1, tzinfo=timezone.utc)
FAULT_FILE = "orders_batch3.jsonl"
FAULT_LABEL = f"SYNTHETIC injected commit fault on {FAULT_FILE}; NOT a real outage"


def prepare_workdir(workdir: Path) -> None:
    """Create/reset the demo directory. Only ever deletes a directory this demo created (marker file).

    A missing or EMPTY existing directory is fine; a non-empty directory without the marker (or a plain file) is refused.
    """
    workdir = workdir.resolve()
    if workdir.exists():
        if not workdir.is_dir():
            raise SystemExit(f"refusing to use {workdir}: it is not a directory")
        if (workdir / MARKER).is_file():
            shutil.rmtree(workdir)
        elif any(workdir.iterdir()):
            raise SystemExit(f"refusing to reuse {workdir}: not empty and not created by this demo "
                             f"(no {MARKER} marker)")
    workdir.mkdir(parents=True, exist_ok=True)
    (workdir / MARKER).write_text("created by `python -m retail_ops demo`; safe to delete\n")


ALERT = "pipeline_run_failed"
RUN_IDS = ("run-001", "run-002", "run-003", "run-004")
RUN_PATTERNS = ("*_batch1.*", "*_batch[12].*", "*", "*")  # file patterns of runs 1-4 (run 4 is skipped with --leave-firing)


def verify_demo(run_events: list[Event], alert_events: list[Event], leave_firing: bool) -> list[str]:
    """Check the RECORDED events (what a reader of the logs would see) against the expected story.

    Returns the problems found (empty = the demo really did: 2 good loads, a failing faulted run that FIRED the alert,
    and - unless leave_firing - a good recovery run that RESOLVED it). Absence of an active alert is not enough.
    """
    problems = [f"invalid event: {p}" for e in run_events + alert_events for p in validate_event(e)]
    fin = {str(e["run_id"]): e for e in run_events if e.get("event") == "run_finished"}
    expected_runs = RUN_IDS[:3] if leave_firing else RUN_IDS
    for rid in expected_runs:
        if rid not in fin:
            problems.append(f"{rid}: no run_finished event recorded")
    for rid in expected_runs:
        f = fin.get(rid)
        if f is None:
            continue
        files = f.get("files") or {}
        if rid == "run-003":
            if f.get("ok") is not False or files.get("failed", 0) < 1 or not f.get("synthetic_fault"):
                problems.append(f"run-003 should FAIL with a labelled synthetic fault and >=1 failed file (got ok={f.get('ok')}, files={files})")
        else:
            if f.get("ok") is not True or files.get("failed", 0) != 0 or files.get("loaded", 0) < 1:
                problems.append(f"{rid} should succeed and load >=1 file (got ok={f.get('ok')}, files={files})")
    transitions = [(e["event"], e["run_id"], e.get("alert")) for e in alert_events]
    want = [("alert_fired", "run-003", ALERT)] + ([] if leave_firing else [("alert_resolved", "run-004", ALERT)])
    if transitions != want:
        problems.append(f"alert transitions {transitions} != expected {want}")
    fired = [e for e in alert_events if e.get("event") == "alert_fired"]
    if fired and not fired[0].get("synthetic_fault"):
        problems.append("alert_fired is missing the synthetic_fault label")
    mirrored = [(e["event"], e["run_id"]) for e in run_events if str(e.get("event", "")).startswith("alert_")]
    if mirrored != [(t[0], t[1]) for t in want]:
        problems.append(f"alert events in the run log {mirrored} do not match the alert file")
    still = sorted(firing_alerts(alert_events))
    if still != ([ALERT] if leave_firing else []):
        problems.append(f"alerts firing at the end: {still}")
    return problems


def write_artifacts(workdir: Path, theme: str = "auto") -> tuple[Path, Path]:
    events = load_events(workdir)
    prom, page = workdir / "metrics.prom", workdir / "dashboard.html"
    prom.write_text(render_metrics(events), encoding="utf-8")
    page.write_text(render_html(events, 0, theme), encoding="utf-8")
    return prom, page


def _line(o: RunOutcome, note: str) -> str:
    st = "SUCCESS" if o.ok else "FAILED "
    files = ", ".join(f"{k}={v}" for k, v in sorted((o.finished["files"]).items()))
    return f"  {o.run_id}  [{st}]  {files}  {note}"


@dataclass
class DemoResult:
    workdir: Path
    outcomes: list[RunOutcome] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    events: list[Event] = field(default_factory=list)
    alert_events: list[Event] = field(default_factory=list)
    prom: Path | None = None
    page: Path | None = None

    @property
    def ok(self) -> bool:
        return not self.problems


def run_demo(workdir: Path, *, leave_firing: bool = False, say=print) -> int:
    return 0 if execute_demo(workdir, leave_firing=leave_firing, say=say).ok else 1


def execute_demo(workdir: Path, *, leave_firing: bool = False, say=print, on_file: Callable[[int, FileReport], None] | None = None,
                 theme: str = "auto", on_ready: Callable[[Path], None] | None = None) -> DemoResult:
    """Run the demo and VERIFY it from the recorded logs. ``on_file(run_number, report)`` is called per processed file."""
    plog = logging.getLogger("pipeline")  # retry warnings would clutter the demo; attempts are in the events
    old_level, plog.level = plog.level, logging.CRITICAL
    try:
        return _run_demo(workdir, leave_firing, say, on_file, theme, on_ready)
    finally:
        plog.setLevel(old_level)


def _run_demo(workdir: Path, leave_firing: bool, say, on_file, theme: str, on_ready) -> DemoResult:
    prepare_workdir(workdir)
    workdir = workdir.resolve()
    batches, db = workdir / "batches", workdir / "warehouse.duckdb"
    log = Emitter(workdir / RUN_LOG)
    alerts = LocalAlertManager(workdir / "logs" / "alerts.jsonl", also=log)
    retry = RetryPolicy(max_attempts=3, base_delay=0.02)
    generate_demo_batches(batches, seed=42)
    if on_ready:
        on_ready(batches)  # batches exist: callers can now count the files each run will process
    say("=" * 78)
    say(" Retail pipeline - LOCAL monitoring demo   (SYNTHETIC data, no Docker, no network)")
    say("=" * 78)
    say(f" workdir: {workdir}\n")

    result = DemoResult(workdir)

    def one(n: int, trigger: str, pattern: str = "*", fault: bool = False) -> RunOutcome:
        wh = open_warehouse("duckdb", db)
        plan = FaultPlan(wh, {FAULT_FILE: 99}).install() if fault else None
        try:
            out = execute_run(wh, batches, run_id=f"run-{n:03d}", emit=log, trigger=trigger, retry=retry,
                              pattern=pattern, as_of=AS_OF, synthetic_fault=FAULT_LABEL if fault else None,
                              on_file=(lambda f: on_file(n, f)) if on_file else None)
        finally:
            if plan:
                plan.remove()
            wh.close()
        result.outcomes.append(out)
        alert = alerts.evaluate(out.finished)
        say(_line(out, f"({trigger})"))
        if alert:
            tag = "ALERT FIRED   " if alert["event"] == "alert_fired" else "ALERT RESOLVED"
            say(f"      >>> {tag} {alert['alert']}  delivery={alert['delivery']}  "
                f"{'[SYNTHETIC FAULT] ' if alert.get('synthetic_fault') else ''}")
        return out

    say("1/4 first load (success)")
    one(1, "demo: batch1 only", RUN_PATTERNS[0])
    say("2/4 load batch 2 (success)")
    one(2, "demo: batch1+batch2", RUN_PATTERNS[1])
    say(f"3/4 SYNTHETIC FAULT: commits of {FAULT_FILE} are made to fail (pipeline retries 3x, then reports failure)")
    one(3, "demo: all batches + synthetic fault", RUN_PATTERNS[2], fault=True)
    if not leave_firing:
        say("4/4 recovery run without the fault")
        one(4, "demo: recovery", RUN_PATTERNS[3])
    prom, page = write_artifacts(workdir, theme)
    events = read_events(workdir / RUN_LOG)
    alert_events = read_events(workdir / "logs" / "alerts.jsonl")
    result.problems = verify_demo(events, alert_events, leave_firing)
    result.events, result.alert_events, result.prom, result.page = events, alert_events, prom, page
    still = list(firing_alerts(alert_events))
    say("\n" + "-" * 78)
    say(f" structured run log : {workdir / RUN_LOG}   ({len(events)} JSON events)")
    say(f" local alert file   : {workdir / 'logs' / 'alerts.jsonl'}   (LOCAL ONLY, nothing was sent)")
    say(f" prometheus metrics : {prom}")
    say(f" static dashboard   : {page}")
    say(f" alerts firing now  : {still or 'none'}")
    say("-" * 78)
    say(" Live dashboard (loopback only):  python -m retail_ops dashboard --workdir " + str(workdir))
    say(" then open http://127.0.0.1:8765/   (Ctrl-C to stop)")
    if result.ok:
        say(" RESULT: demo invariants held (runs 1-2 succeeded, run 3 failed with the SYNTHETIC fault and the local alert FIRED"
            + ("; alert still firing" if leave_firing else "; run 4 succeeded and the alert RESOLVED") + ")")
    else:
        say(" RESULT: demo invariants FAILED")
        for p in result.problems:
            say(f"   - {p}")
    return result
