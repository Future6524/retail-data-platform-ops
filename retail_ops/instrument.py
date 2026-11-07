"""Run the real pipeline and describe it as structured events."""
from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from pipeline.retry import RetryPolicy
from pipeline.runner import FileReport, RunReport, run_pipeline
from pipeline.warehouse.base import SqlWarehouse

from .events import Clock, Event, make_event, utc_now


@dataclass
class RunOutcome:
    run_id: str
    ok: bool
    report: RunReport | None
    error: str | None
    finished: Event

    @property
    def changed_state(self) -> bool:
        """True if at least one file committed a checkpoint (loaded/rejected) during this run."""
        return bool(self.report and any(f.status in ("loaded", "rejected") for f in self.report.files))


def execute_run(wh: SqlWarehouse, input_dir: Path, *, run_id: str, emit: Callable[[Event], None],
                trigger: str = "manual", clock: Clock = utc_now, retry: RetryPolicy | None = None,
                pattern: str = "*", as_of=None, synthetic_fault: str | None = None,
                monotonic: Callable[[], float] = time.monotonic,
                on_file: Callable[[FileReport], None] | None = None) -> RunOutcome:
    """Run the pipeline once, emitting run_started / file_result* / run_finished. Never raises for pipeline errors.

    ``synthetic_fault`` is a human label attached to the events when the caller deliberately injected a fault.
    """
    extra = {"synthetic_fault": synthetic_fault} if synthetic_fault else {}
    emit(make_event("run_started", run_id, clock, trigger=trigger, **extra))
    t0 = monotonic()
    report: RunReport | None = None
    error: str | None = None
    try:
        report = run_pipeline(wh, input_dir, as_of=as_of, pattern=pattern, retry=retry or RetryPolicy(),
                            on_file=on_file)
    except Exception as exc:  # noqa: BLE001 - a crash must become a failed-run event, not a lost run
        error = f"{type(exc).__name__}: {exc}"[:500]
    files: dict[str, int] = {}
    if report is not None:
        for f in report.files:
            files[f.status] = files.get(f.status, 0) + 1
            emit(make_event("file_result", run_id, clock, file=f.file, entity=f.entity, status=f.status,
                            attempts=f.attempts, counts=f.counts, error=f.error, **extra))
    ok = report is not None and report.ok
    if report is not None and not report.ok and error is None:
        failed = [f for f in report.files if f.status == "failed"]
        error = f"{len(failed)} file(s) failed: " + ", ".join(f.file for f in failed)
    finished = make_event("run_finished", run_id, clock, ok=ok, outcome="success" if ok else "failure", duration_seconds=round(monotonic() - t0, 3),
                          files=files, totals=report.totals() if report else {}, error=error,
                          ignored=len(report.ignored) if report else 0, **extra)
    emit(finished)
    return RunOutcome(run_id, ok, report, error, finished)
