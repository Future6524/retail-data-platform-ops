"""Project 2 showcase: ``python -m retail_ops showcase`` (progress bar + summary table + light-mode dashboard.html).

It runs the real monitoring demo (real pipeline, SYNTHETIC data, one labelled SYNTHETIC injected fault, LOCAL-ONLY alert)
and verifies it from the recorded logs. The bar advances once per file actually processed; every table value comes from
the recorded events. No browser is opened and no server is started: the open commands are only printed.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import TextIO

from pipeline.runner import FileReport
from pipeline.showcase import count_files
from pipeline.ui import ProgressBar, browser_command, table_for

from .demo import RUN_PATTERNS, execute_demo
from .events import Event

HEADERS = ["Run", "Scenario", "Files", "Loaded", "Skip", "Reject", "Fail", "Insert", "Quar.", "Secs", "Result", "Local alert"]
LIVE_URL = "http://127.0.0.1:8765/"
LABELS = {"run-001": "batch 1 load", "run-002": "batches 1-2", "run-003": "SYNTHETIC fault", "run-004": "recovery"}


def summary_rows(events: list[Event], alert_events: list[Event]) -> list[list[object]]:
    alerts = {str(a["run_id"]): ("FIRED" if a["event"] == "alert_fired" else "RESOLVED") for a in alert_events}
    rows: list[list[object]] = []
    for e in events:
        if e.get("event") != "run_finished":
            continue
        f, t = e.get("files") or {}, e.get("totals") or {}
        rid = str(e["run_id"])
        rows.append([rid, LABELS.get(rid, ""), sum(f.values()), f.get("loaded", 0), f.get("skipped_checkpointed", 0),
                     f.get("rejected", 0), f.get("failed", 0), t.get("rows_inserted", 0), t.get("rows_quarantined", 0),
                     f"{e.get('duration_seconds', 0):.2f}", "ok" if e.get("ok") else "FAILED (fault)", alerts.get(rid, "-")])
    return rows


def run_showcase(workdir: Path, out: TextIO | None = None, *, leave_firing: bool = False) -> int:
    out = out or sys.stdout
    print("Retail platform monitoring showcase  -  SYNTHETIC data, real pipeline, LOCAL-ONLY alerts (nothing is sent)", file=out)
    patterns = RUN_PATTERNS[:3] if leave_firing else RUN_PATTERNS
    bar = ProgressBar(0, out)

    def ready(batches: Path) -> None:
        bar.total = sum(count_files(batches, p) for p in patterns)

    def tick(n: int, f: FileReport) -> None:
        bar.advance(f"run {n}/{len(patterns)} {LABELS[f'run-{n:03d}']}", f"{f.file} {f.status}")

    res = execute_demo(workdir, leave_firing=leave_firing, say=lambda *_a, **_k: None, on_file=tick, theme="light",
                       on_ready=ready)
    bar.finish("done")
    rows = summary_rows(res.events, res.alert_events)
    print("", file=out)
    print(table_for(out, HEADERS, rows, right=tuple(range(2, 10))), file=out)
    transitions = ", ".join(f"{a['event'][6:].upper()} {a['alert']} at {a['run_id']}" for a in res.alert_events) or "none"
    nfiles = sum(1 for e in res.events if e.get("event") == "file_result")
    print(f"\nlocal alert transitions: {transitions}   |   firing now: "
          f"{', '.join(sorted(a['alert'] for a in res.alert_events if a['event'] == 'alert_fired')) if leave_firing else 'none'}",
          file=out)
    print(f"{len(res.events)} structured events, {nfiles} file results", file=out)
    if res.page:
        print(f"dashboard (static HTML) : {res.page}", file=out)
        print(f"open it                 : {browser_command(res.page)}", file=out)
        print(f"live dashboard (separate, loopback only, Ctrl-C to stop): "
              f"python -m retail_ops dashboard --workdir {workdir} --theme light", file=out)
        print(f"then open               : python -m webbrowser -t {LIVE_URL}", file=out)
    if res.ok:
        print("RESULT: verified from the recorded logs - good loads, the faulted run failed, the alert fired"
              + ("" if leave_firing else " and resolved on recovery"), file=out)
        return 0
    print("RESULT: showcase checks FAILED", file=out)
    for p in res.problems:
        print(f"  - {p}", file=out)
    return 1
