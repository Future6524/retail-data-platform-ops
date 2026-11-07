"""Dashboard HTML + real loopback HTTP requests (server runs in a thread and is always shut down), and the demo."""
import json
import threading
import urllib.error
import urllib.request
from datetime import datetime, timezone

import pytest

from retail_ops import demo as demo_mod
from retail_ops.cli import healthcheck, main
from retail_ops.dashboard import is_loopback, make_server, render_html, summarize_runs
from retail_ops.events import make_event, read_events, validate_event
from retail_ops.metrics import parse_metrics


def clock():
    return datetime(2026, 3, 1, tzinfo=timezone.utc)


def evs(*, ok=True, error=None, fault=None, rid="r1"):
    extra = {"synthetic_fault": fault} if fault else {}
    return [make_event("run_started", rid, clock, trigger="t", **extra),
            make_event("run_finished", rid, clock, ok=ok, outcome="success" if ok else "failure", duration_seconds=0.5,
                       files={"loaded": 2}, totals={"rows_inserted": 7, "rows_quarantined": 1}, error=error, **extra)]


def test_html_shows_runs_counters_and_synthetic_labels():
    page = render_html(evs() + evs(ok=False, error="boom", fault="SYNTHETIC fault X", rid="r2") + [
        make_event("alert_fired", "r2", clock, alert="pipeline_run_failed", severity="critical",
                   summary="run r2 failed", delivery="local-file-only", synthetic_fault="SYNTHETIC fault X")])
    for needle in ("SYNTHETIC", "SUCCESS", "FAILED", "ALERT FIRING", "pipeline_run_failed", "local-file-only",
                   "SYNTHETIC FAULT", "SYNTHETIC fault X", "rows inserted", "r1", "r2"):
        assert needle in page, needle
    assert "<script" not in page.lower() and "http://" not in page and "https://" not in page  # offline, no JS
    assert 'http-equiv="refresh"' not in page and 'http-equiv="refresh"' in render_html([], 5)


def test_html_escapes_hostile_values():
    page = render_html(evs(ok=False, error="<script>alert(1)</script>", rid="r<b>1"))
    assert "<script>alert(1)</script>" not in page and "&lt;script&gt;alert(1)&lt;/script&gt;" in page
    assert "r<b>1" not in page


def test_empty_state_is_helpful():
    assert "No runs recorded" in render_html([]) and "No alerts firing" in render_html([])
    assert summarize_runs([]) == []


@pytest.fixture
def served(tmp_path):
    (tmp_path / "logs").mkdir()
    with (tmp_path / "logs" / "runs.jsonl").open("w") as fh:
        for e in evs() + evs(ok=False, error="boom", fault="SYNTHETIC fault X", rid="r2"):
            fh.write(json.dumps(e) + "\n")
    srv = make_server(tmp_path, "127.0.0.1", 0)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
    srv.server_close()
    t.join(5)
    assert not t.is_alive()


def get(url, method="GET", headers=None):
    req = urllib.request.Request(url, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def test_http_routes(served):
    st, h, body = get(served + "/")
    assert st == 200 and h["Content-Type"].startswith("text/html") and b"FAILED" in body and b"SYNTHETIC" in body
    assert "default-src 'none'" in h["Content-Security-Policy"] and h["X-Content-Type-Options"] == "nosniff"
    st, h, body = get(served + "/metrics?x=1")
    assert st == 200 and parse_metrics(body.decode())["retail_pipeline_runs_total"]['{status="failure"}'] == 1
    st, _, body = get(served + "/api/runs")
    assert st == 200 and [r["run_id"] for r in json.loads(body)] == ["r1", "r2"]
    st, _, body = get(served + "/api/alerts")
    assert st == 200 and json.loads(body)["firing"] == []
    assert get(served + "/healthz")[2] == b'{"status":"ok"}'
    st, _, body = get(served + "/", "HEAD")
    assert st == 200 and body == b""


def test_http_rejects_bad_paths_methods_and_hosts(served):
    assert get(served + "/nope")[0] == 404
    assert get(served + "/../../../etc/passwd")[0] == 404
    assert get(served + "/%2e%2e/%2e%2e/etc/passwd")[0] == 404
    assert get(served + "/", "POST")[0] == 405 and get(served + "/", "DELETE")[0] == 405
    st, _, _ = get(served + "/", headers={"Host": "evil.example"})  # DNS-rebinding guard
    assert st == 403
    assert get(served + "/", headers={"Host": "localhost:1234"})[0] == 200


def test_bind_policy_defaults_to_loopback(tmp_path):
    assert is_loopback("127.0.0.1") and is_loopback("::1") and is_loopback("localhost")
    assert not is_loopback("0.0.0.0") and not is_loopback("192.168.1.5") and not is_loopback("example.com")
    with pytest.raises(ValueError, match="refusing to bind"):
        make_server(tmp_path, "0.0.0.0", 0)
    srv = make_server(tmp_path, port=0)
    try:
        assert srv.server_address[0] == "127.0.0.1"
    finally:
        srv.server_close()
    assert main(["dashboard", "--workdir", str(tmp_path), "--host", "0.0.0.0", "--port", "0"]) == 2


# ------------------------------------------------------------------------------------------------- the demo
def test_demo_success_failure_alert_and_recovery(tmp_path, capsys):
    wd = tmp_path / "mon"
    assert demo_mod.run_demo(wd) == 0
    out = capsys.readouterr().out
    assert "ALERT FIRED" in out and "ALERT RESOLVED" in out and "SYNTHETIC FAULT" in out and "LOCAL ONLY" in out
    events = read_events(wd / "logs" / "runs.jsonl")
    assert all(validate_event(e) == [] for e in events)
    fin = [e for e in events if e["event"] == "run_finished"]
    assert [(e["run_id"], e["ok"]) for e in fin] == [("run-001", True), ("run-002", True), ("run-003", False),
                                                     ("run-004", True)]
    assert "NOT a real outage" in fin[2]["synthetic_fault"] and "synthetic_fault" not in fin[3]
    alerts = read_events(wd / "logs" / "alerts.jsonl")
    assert [(a["event"], a["run_id"], a["delivery"]) for a in alerts] == [
        ("alert_fired", "run-003", "local-file-only"), ("alert_resolved", "run-004", "local-file-only")]
    m = parse_metrics((wd / "metrics.prom").read_text())
    assert m["retail_pipeline_runs_total"] == {'{status="success"}': 3, '{status="failure"}': 1}
    assert "SYNTHETIC FAULT" in (wd / "dashboard.html").read_text()
    assert healthcheck(wd, 1)[0] is True


def test_demo_is_repeatable_and_leave_firing_variant(tmp_path):
    wd = tmp_path / "mon"
    sig = []
    for _ in range(2):
        assert demo_mod.run_demo(wd, say=lambda *_: None) == 0
        sig.append([(e["event"], e["run_id"], e.get("ok"), e.get("files")) for e in read_events(wd / "logs" / "runs.jsonl")])
    assert sig[0] == sig[1]  # same structure each time, old run history replaced rather than appended
    assert demo_mod.run_demo(wd, leave_firing=True, say=lambda *_: None) == 0
    assert "ALERT FIRING" in (wd / "dashboard.html").read_text()
    ok, info = healthcheck(wd, None)
    assert not ok and "FAIL" in info["checks"]["last_run"]


def test_demo_refuses_to_clobber_foreign_directories(tmp_path):
    foreign = tmp_path / "mine"
    foreign.mkdir()
    (foreign / "precious.txt").write_text("keep")
    with pytest.raises(SystemExit):
        demo_mod.run_demo(foreign, say=lambda *_: None)
    assert (foreign / "precious.txt").read_text() == "keep"


def test_demo_does_not_leave_fault_plan_installed(tmp_path):
    demo_mod.run_demo(tmp_path / "d", say=lambda *_: None)
    from pipeline.warehouse import open_warehouse
    wh = open_warehouse("duckdb")
    assert "_commit" not in wh.__dict__  # faults live only inside the demo's own warehouse handles


def test_cli_render_metrics_and_healthcheck(tmp_path, capsys):
    assert main(["render", "--workdir", str(tmp_path)]) == 2  # nothing recorded yet: clear error, no crash
    demo_mod.run_demo(tmp_path / "w", say=lambda *_: None)
    capsys.readouterr()
    assert main(["render", "--workdir", str(tmp_path / "w")]) == 0
    assert main(["metrics", "--workdir", str(tmp_path / "w")]) == 0
    assert "retail_pipeline_runs_total" in capsys.readouterr().out
    assert main(["healthcheck"]) == 0
    assert json.loads(capsys.readouterr().out)["checks"]["duckdb"] == "ok"
    assert main(["healthcheck", "--workdir", str(tmp_path / "empty")]) == 1
