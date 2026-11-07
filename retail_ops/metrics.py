"""Prometheus text-format metrics derived from the structured event log (stateless: replay the log).

Counters are therefore monotonic for the life of the log file, like a process that never restarts. The same
text is served at /metrics by the dashboard and can be dropped in a node_exporter textfile-collector directory.
"""
from __future__ import annotations

from .alerts import firing_alerts
from .events import Event

FILE_STATUSES = ("loaded", "rejected", "skipped_checkpointed", "failed")
ROW_KINDS = ("rows_read", "rows_inserted", "rows_updated", "rows_quarantined")


def _esc(v: str) -> str:
    return v.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _sample(name: str, value: float | int, **labels: str) -> str:
    lab = ",".join(f'{k}="{_esc(v)}"' for k, v in labels.items())
    return f"{name}{{{lab}}} {value}" if lab else f"{name} {value}"


def _epoch(ts: str) -> float:
    from datetime import datetime
    return datetime.fromisoformat(ts).timestamp()


def render_metrics(events: list[Event]) -> str:
    finished = [e for e in events if e.get("event") == "run_finished"]
    ok_runs = [e for e in finished if e.get("ok")]
    files = [e for e in events if e.get("event") == "file_result"]
    fired = [e for e in events if e.get("event") == "alert_fired"]
    last = finished[-1] if finished else None
    out: list[str] = []

    def metric(name: str, kind: str, help_: str, samples: list[str]) -> None:
        out.extend([f"# HELP {name} {help_}", f"# TYPE {name} {kind}", *samples])

    metric("retail_pipeline_runs_total", "counter", "Pipeline runs by outcome (SYNTHETIC data).",
           [_sample("retail_pipeline_runs_total", len(ok_runs), status="success"),
            _sample("retail_pipeline_runs_total", len(finished) - len(ok_runs), status="failure")])
    metric("retail_pipeline_files_total", "counter", "Per-file results across all runs.",
           [_sample("retail_pipeline_files_total", sum(1 for f in files if f.get("status") == s), status=s)
            for s in FILE_STATUSES])
    metric("retail_pipeline_rows_total", "counter", "Rows by kind across all runs.",
           [_sample("retail_pipeline_rows_total", sum(int((f.get("counts") or {}).get(k, 0)) for f in files), kind=k)
            for k in ROW_KINDS])
    metric("retail_pipeline_alerts_fired_total", "counter", "Local alerts fired.",
           [_sample("retail_pipeline_alerts_fired_total", len(fired))])
    metric("retail_pipeline_alerts_firing", "gauge", "Currently firing local alerts (1 = firing).",
           [_sample("retail_pipeline_alerts_firing", 1 if name in firing_alerts(events) else 0, alert=name)
            for name in ["pipeline_run_failed"]])
    metric("retail_pipeline_last_run_success", "gauge", "1 if the most recent run succeeded, else 0.",
           [_sample("retail_pipeline_last_run_success", 1 if last and last.get("ok") else 0)])
    metric("retail_pipeline_last_run_duration_seconds", "gauge", "Duration of the most recent run.",
           [_sample("retail_pipeline_last_run_duration_seconds", float(last["duration_seconds"]) if last else 0)])
    metric("retail_pipeline_last_run_timestamp_seconds", "gauge", "Unix time the most recent run finished (0 = never).",
           [_sample("retail_pipeline_last_run_timestamp_seconds", _epoch(last["ts"]) if last else 0)])
    metric("retail_pipeline_last_success_timestamp_seconds", "gauge",
           "Unix time the most recent successful run finished (0 = never).",
           [_sample("retail_pipeline_last_success_timestamp_seconds", _epoch(ok_runs[-1]["ts"]) if ok_runs else 0)])
    return "\n".join(out) + "\n"


def parse_metrics(text: str) -> dict[str, dict[str, float]]:
    """Minimal strict parser for the subset above (used by tests): {metric: {labelstring: value}}."""
    import re
    result: dict[str, dict[str, float]] = {}
    types: dict[str, str] = {}
    for line in text.splitlines():
        if not line:
            continue
        if line.startswith("# TYPE "):
            _, _, name, kind = line.split(" ", 3)
            types[name] = kind
            continue
        if line.startswith("#"):
            continue
        m = re.fullmatch(r'([a-zA-Z_:][a-zA-Z0-9_:]*)(\{[^}]*\})? (-?[0-9.]+(?:e[+-]?[0-9]+)?)', line)
        if not m:
            raise ValueError(f"bad metric line: {line!r}")
        if m.group(1) not in types:
            raise ValueError(f"sample without TYPE: {m.group(1)}")
        result.setdefault(m.group(1), {})[m.group(2) or ""] = float(m.group(3))
    return result
