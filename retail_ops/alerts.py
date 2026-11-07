"""LOCAL-ONLY alerting. Alerts are appended to a file; nothing here can send e-mail, webhooks or pages.

Rule `pipeline_run_failed`: FIRES on the first failed run, stays firing while runs keep failing (no duplicate
fire events), RESOLVES on the next successful run. State is derived from the alert file, so it survives restarts.
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable

from .events import Clock, Emitter, Event, make_event, read_events, utc_now

ALERT_NAME = "pipeline_run_failed"
DELIVERY = "local-file-only"  # explicit in every record: no external notification channel exists


def firing_alerts(events: list[Event]) -> dict[str, Event]:
    """Latest unresolved alert_fired event per alert name."""
    state: dict[str, Event] = {}
    for e in events:
        if e.get("event") == "alert_fired":
            state[str(e.get("alert"))] = e
        elif e.get("event") == "alert_resolved":
            state.pop(str(e.get("alert")), None)
    return state


class LocalAlertManager:
    def __init__(self, alerts_file: Path, clock: Clock = utc_now, also: Callable[[Event], None] | None = None):
        self.sink, self.clock, self.also = Emitter(alerts_file), clock, also
        self.path = alerts_file

    def _write(self, e: Event) -> Event:
        self.sink(e)
        if self.also:
            self.also(e)
        return e

    def evaluate(self, finished: Event) -> Event | None:
        """Feed a run_finished event; returns the alert event emitted (if any)."""
        run_id = str(finished["run_id"])
        firing = ALERT_NAME in firing_alerts(read_events(self.path))
        fault = finished.get("synthetic_fault")
        extra = {"synthetic_fault": fault} if fault else {}
        if not finished.get("ok") and not firing:
            label = f" [{fault}]" if fault else ""
            return self._write(make_event(
                "alert_fired", run_id, self.clock, alert=ALERT_NAME, severity="critical", delivery=DELIVERY,
                summary=f"pipeline run {run_id} failed: {finished.get('error')}{label}", **extra))
        if finished.get("ok") and firing:
            return self._write(make_event(
                "alert_resolved", run_id, self.clock, alert=ALERT_NAME, delivery=DELIVERY,
                summary=f"pipeline run {run_id} succeeded; failure alert resolved"))
        return None
