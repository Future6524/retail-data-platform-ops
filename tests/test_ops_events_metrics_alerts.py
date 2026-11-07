"""Structured log schema, Prometheus metrics, and local alert fire/hold/resolve behaviour."""
import json
from datetime import datetime, timezone

import pytest
from conftest import AS_OF, FAST, jl, order, write

from pipeline.faults import FaultPlan
from retail_ops.alerts import ALERT_NAME, DELIVERY, LocalAlertManager, firing_alerts
from retail_ops.events import Emitter, make_event, read_events, validate_event
from retail_ops.instrument import execute_run
from retail_ops.metrics import parse_metrics, render_metrics


def clock():
    return datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def inputs(tmp_path):
    d = tmp_path / "in"
    d.mkdir()
    write(d, "orders_a.jsonl", jl(order("O1"), order("O2")))
    write(d, "orders_b.jsonl", jl(order("O3")))
    return d


def run(wh, inputs, log, rid, fault=None, **kw):
    plan = FaultPlan(wh, {"orders_b.jsonl": 99}).install() if fault else None
    try:
        return execute_run(wh, inputs, run_id=rid, emit=log, clock=clock, retry=FAST, as_of=AS_OF,
                           synthetic_fault="SYNTHETIC test fault" if fault else None, **kw)
    finally:
        if plan:
            plan.remove()


def test_success_run_emits_schema_valid_events(wh, inputs, tmp_path):
    log = Emitter(tmp_path / "runs.jsonl")
    out = run(wh, inputs, log, "r1")
    events = read_events(tmp_path / "runs.jsonl")
    assert [e["event"] for e in events] == ["run_started", "file_result", "file_result", "run_finished"]
    assert all(validate_event(e) == [] for e in events), [validate_event(e) for e in events]
    fin = events[-1]
    assert fin["ok"] is True and fin["outcome"] == "success" and fin["files"] == {"loaded": 2}
    assert fin["totals"]["rows_inserted"] == 3 and out.changed_state
    assert all(e["synthetic_data"] is True and e["schema"] == 1 for e in events)
    assert "synthetic_fault" not in fin


def test_failed_run_carries_fault_label_and_is_valid(wh, inputs, tmp_path):
    log = Emitter(tmp_path / "runs.jsonl")
    out = run(wh, inputs, log, "r1", fault=True)
    assert not out.ok and "orders_b.jsonl" in (out.error or "")
    events = read_events(tmp_path / "runs.jsonl")
    assert all(validate_event(e) == [] for e in events)
    fin = events[-1]
    assert fin["outcome"] == "failure" and fin["synthetic_fault"] == "SYNTHETIC test fault"
    failed = [e for e in events if e["event"] == "file_result" and e["status"] == "failed"]
    assert failed and failed[0]["attempts"] == 3 and "InjectedFault" in failed[0]["error"]


def test_pipeline_crash_becomes_a_failed_run_event(wh, tmp_path):
    missing = tmp_path / "nope"
    log = Emitter(tmp_path / "runs.jsonl")
    out = execute_run(wh, missing, run_id="r1", emit=log, clock=clock, retry=FAST)
    fin = read_events(tmp_path / "runs.jsonl")[-1]
    assert fin["event"] == "run_finished" and fin["ok"] is False and out.error
    assert validate_event(fin) == []


@pytest.mark.parametrize("mutate,expect", [
    (lambda e: e.pop("run_id"), "missing run_id"),
    (lambda e: e.update(schema=2), "unsupported schema"),
    (lambda e: e.update(synthetic_data=False), "synthetic_data must be true"),
    (lambda e: e.update(ok="yes"), "ok has wrong type"),
    (lambda e: e.update(event="mystery"), "unknown event"),
    (lambda e: e.pop("outcome"), "missing outcome"),
])
def test_validate_event_rejects_bad_events(mutate, expect):
    e = make_event("run_finished", "r", clock, ok=True, outcome="success", duration_seconds=1.0, files={}, totals={})
    assert validate_event(e) == []
    mutate(e)
    assert any(expect in p for p in validate_event(e))
    assert validate_event([1]) == ["event is not a JSON object"]


def test_emitter_is_json_lines_and_reader_skips_torn_lines(tmp_path):
    p = tmp_path / "x" / "log.jsonl"
    em = Emitter(p)
    em(make_event("run_started", "r", clock, trigger="t"))
    with p.open("a") as fh:
        fh.write('{"torn": tru')  # half-written last line
    assert len(read_events(p)) == 1
    assert json.loads(p.read_text().splitlines()[0])["event"] == "run_started"


def test_metrics_exposition_is_valid_and_counts_runs(wh, inputs, tmp_path):
    log = Emitter(tmp_path / "runs.jsonl")
    run(wh, inputs, log, "r1")
    run(wh, inputs, log, "r2")
    text = render_metrics(read_events(tmp_path / "runs.jsonl"))
    m = parse_metrics(text)  # strict parser: every sample has a TYPE, every line is well-formed
    assert m["retail_pipeline_runs_total"]['{status="success"}'] == 2
    assert m["retail_pipeline_runs_total"]['{status="failure"}'] == 0
    assert m["retail_pipeline_files_total"]['{status="loaded"}'] == 2
    assert m["retail_pipeline_files_total"]['{status="skipped_checkpointed"}'] == 2
    assert m["retail_pipeline_rows_total"]['{kind="rows_inserted"}'] == 3
    assert m["retail_pipeline_last_run_success"][""] == 1
    assert m["retail_pipeline_last_success_timestamp_seconds"][""] == clock().timestamp()
    assert text.count("# TYPE retail_pipeline_runs_total counter") == 1


def test_metrics_with_no_events_are_still_valid():
    m = parse_metrics(render_metrics([]))
    assert m["retail_pipeline_runs_total"]['{status="success"}'] == 0
    assert m["retail_pipeline_last_run_timestamp_seconds"][""] == 0


def test_parse_metrics_rejects_garbage():
    with pytest.raises(ValueError):
        parse_metrics("not a metric line\n")
    with pytest.raises(ValueError):
        parse_metrics("untyped_metric 1\n")


def test_alert_fires_once_stays_firing_and_resolves(wh, inputs, tmp_path):
    log = Emitter(tmp_path / "runs.jsonl")
    alerts = LocalAlertManager(tmp_path / "alerts.jsonl", clock=clock, also=log)

    def step(rid, fault, **kw):
        return alerts.evaluate(run(wh, inputs, log, rid, fault=fault, **kw).finished)

    assert step("r1", False, pattern="orders_a*") is None                                  # healthy: nothing
    fired = step("r2", True)                                          # failure: FIRES
    assert fired and fired["event"] == "alert_fired" and fired["alert"] == ALERT_NAME
    assert fired["delivery"] == DELIVERY == "local-file-only"
    assert fired["synthetic_fault"] == "SYNTHETIC test fault" and "[SYNTHETIC test fault]" in fired["summary"]
    assert validate_event(fired) == []
    assert ALERT_NAME in firing_alerts(read_events(tmp_path / "runs.jsonl"))
    assert parse_metrics(render_metrics(read_events(tmp_path / "runs.jsonl")))[
        "retail_pipeline_alerts_firing"]['{alert="pipeline_run_failed"}'] == 1
    assert step("r3", True) is None                                   # still failing: no duplicate fire
    resolved = step("r4", False)                                      # recovery: RESOLVES
    assert resolved and resolved["event"] == "alert_resolved" and validate_event(resolved) == []
    assert firing_alerts(read_events(tmp_path / "runs.jsonl")) == {}
    assert step("r5", False) is None
    kinds = [e["event"] for e in read_events(tmp_path / "alerts.jsonl")]
    assert kinds == ["alert_fired", "alert_resolved"]
    m = parse_metrics(render_metrics(read_events(tmp_path / "runs.jsonl")))
    assert m["retail_pipeline_alerts_fired_total"][""] == 1
    assert m["retail_pipeline_alerts_firing"]['{alert="pipeline_run_failed"}'] == 0


def test_alert_state_survives_a_new_manager_instance(tmp_path):
    f = tmp_path / "alerts.jsonl"
    bad = make_event("run_finished", "r1", clock, ok=False, outcome="failure", duration_seconds=0, files={}, totals={},
                     error="boom")
    assert LocalAlertManager(f, clock).evaluate(bad)["event"] == "alert_fired"
    assert LocalAlertManager(f, clock).evaluate(bad) is None  # new process, same file: still one alert
    assert not hasattr(LocalAlertManager, "send") and "smtp" not in LocalAlertManager.__module__
