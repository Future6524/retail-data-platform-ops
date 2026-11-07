"""Structured JSON event log: one JSON object per line, schema version 1.

Common fields on every event: schema, ts (UTC ISO-8601), event, service, run_id, synthetic_data (always true).
Event-specific required fields are listed in EVENT_FIELDS and enforced by validate_event().
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, TextIO

from . import SERVICE

SCHEMA_VERSION = 1
Clock = Callable[[], datetime]
Event = dict[str, Any]


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


COMMON_FIELDS = {"schema": int, "ts": str, "event": str, "service": str, "run_id": str, "synthetic_data": bool}
EVENT_FIELDS: dict[str, dict[str, type | tuple[type, ...]]] = {
    "run_started": {"trigger": str},
    "file_result": {"file": str, "entity": str, "status": str, "attempts": int, "counts": dict},
    "run_finished": {"ok": bool, "outcome": str, "duration_seconds": (int, float), "files": dict, "totals": dict},
    "alert_fired": {"alert": str, "severity": str, "summary": str, "delivery": str},
    "alert_resolved": {"alert": str, "summary": str, "delivery": str},
}


def make_event(event: str, run_id: str, clock: Clock = utc_now, **fields: Any) -> Event:
    return {"schema": SCHEMA_VERSION, "ts": clock().isoformat(timespec="milliseconds"), "event": event,
            "service": SERVICE, "run_id": run_id, "synthetic_data": True, **fields}


def validate_event(e: object) -> list[str]:
    """Return a list of schema problems (empty list = valid)."""
    if not isinstance(e, dict):
        return ["event is not a JSON object"]
    problems = []
    for name, ctyp in COMMON_FIELDS.items():
        if name not in e:
            problems.append(f"missing {name}")
        elif not isinstance(e[name], ctyp) or (ctyp is int and isinstance(e[name], bool)):
            problems.append(f"{name} has wrong type")
    if e.get("schema") != SCHEMA_VERSION:
        problems.append(f"unsupported schema {e.get('schema')!r}")
    if e.get("synthetic_data") is not True:
        problems.append("synthetic_data must be true")
    required = EVENT_FIELDS.get(str(e.get("event")))
    if required is None:
        problems.append(f"unknown event {e.get('event')!r}")
        return problems
    for name, typ in required.items():
        if name not in e:
            problems.append(f"{e['event']}: missing {name}")
        elif not isinstance(e[name], typ):
            problems.append(f"{e['event']}: {name} has wrong type")
    return problems


class Emitter:
    """Writes events as JSON lines to a file and/or a stream (stdout in the container). Flushes every line."""

    def __init__(self, path: Path | None = None, stream: TextIO | None = None):
        self.path, self.stream = path, stream
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)

    def __call__(self, event: Event) -> None:
        line = json.dumps(event, sort_keys=True, ensure_ascii=True, default=str)
        if self.path is not None:
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        if self.stream is not None:
            self.stream.write(line + "\n")
            self.stream.flush()


def stdout_emitter() -> Emitter:
    return Emitter(stream=sys.stdout)


def read_events(path: Path) -> list[Event]:
    """Read a JSONL event file; unparseable lines are skipped (a torn last line must not break the dashboard)."""
    if not path.is_file():
        return []
    out: list[Event] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict):
            out.append(obj)
    return out
