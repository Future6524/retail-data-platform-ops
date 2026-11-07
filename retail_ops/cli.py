"""python -m retail_ops <demo|showcase|dashboard|render|metrics|healthcheck|task>"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from .dashboard import RUN_LOG, is_loopback, load_events, make_server, render_html
from .metrics import render_metrics


def healthcheck(workdir: Path | None, max_age_hours: float | None) -> tuple[bool, dict]:
    """Liveness of the install (imports + DuckDB works) and, with --workdir, of the most recent recorded run."""
    info: dict = {"checks": {}}
    try:
        import duckdb
        duckdb.connect(":memory:").execute("select 1").fetchall()
        from pipeline.warehouse import open_warehouse
        open_warehouse("duckdb", ":memory:").close()
        info["checks"]["duckdb"] = "ok"
    except Exception as exc:  # noqa: BLE001
        info["checks"]["duckdb"] = f"FAIL: {type(exc).__name__}: {exc}"
    if workdir is not None:
        runs = [e for e in load_events(workdir) if e.get("event") == "run_finished"]
        if not runs:
            info["checks"]["last_run"] = "FAIL: no runs recorded"
        else:
            last = runs[-1]
            age_h = (datetime.now(timezone.utc) - datetime.fromisoformat(last["ts"])).total_seconds() / 3600
            if not last.get("ok"):
                info["checks"]["last_run"] = f"FAIL: last run {last['run_id']} failed"
            elif max_age_hours is not None and age_h > max_age_hours:
                info["checks"]["last_run"] = f"FAIL: last success is {age_h:.1f}h old (max {max_age_hours}h)"
            else:
                info["checks"]["last_run"] = f"ok: {last['run_id']} ({age_h:.2f}h ago)"
    ok = all(v.startswith("ok") for v in info["checks"].values())
    info["ok"] = ok
    return ok, info


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m retail_ops", description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    wd = ".demo/monitoring"
    d = sub.add_parser("demo", help="run the local success + synthetic-failure monitoring demo")
    d.add_argument("--workdir", default=wd)
    d.add_argument("--leave-firing", action="store_true", help="skip the recovery run so the alert stays firing")
    sh = sub.add_parser("showcase", help="demo with a progress bar, summary table and light-mode dashboard.html to open")
    sh.add_argument("--workdir", default=".demo/ops-showcase")
    sh.add_argument("--leave-firing", action="store_true", help="skip the recovery run so the alert stays firing")
    s = sub.add_parser("dashboard", help="serve the local dashboard (loopback by default)")
    s.add_argument("--workdir", default=wd)
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8765)
    s.add_argument("--allow-non-loopback", action="store_true")
    s.add_argument("--theme", choices=("auto", "light"), default="auto")
    r = sub.add_parser("render", help="write a static dashboard.html (and metrics.prom) from the run log")
    r.add_argument("--workdir", default=wd)
    m = sub.add_parser("metrics", help="print Prometheus metrics derived from the run log")
    m.add_argument("--workdir", default=wd)
    h = sub.add_parser("healthcheck", help="exit 0 if the install works (and the last recorded run is healthy)")
    h.add_argument("--workdir")
    h.add_argument("--max-age-hours", type=float)
    sub.add_parser("task", help="AWS scheduled-task entrypoint (needs S3 env; see retail_ops/task.py)")
    a = ap.parse_args(argv)

    if a.cmd == "demo":
        from .demo import run_demo
        return run_demo(Path(a.workdir), leave_firing=a.leave_firing)
    if a.cmd == "showcase":
        from .showcase import run_showcase
        return run_showcase(Path(a.workdir), leave_firing=a.leave_firing)
    if a.cmd == "dashboard":
        try:
            srv = make_server(Path(a.workdir), a.host, a.port, allow_non_loopback=a.allow_non_loopback, theme=a.theme)
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        host, port = str(srv.server_address[0]), srv.server_address[1]
        print(f"dashboard on http://{host}:{port}/  (workdir {a.workdir}; Ctrl-C to stop)"
              + ("" if is_loopback(host) else "  WARNING: not loopback, unauthenticated"))
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            print("\nstopped")
        finally:
            srv.server_close()
        return 0
    if a.cmd == "render":
        w = Path(a.workdir)
        ev = load_events(w)
        if not ev:
            print(f"error: no events in {w / RUN_LOG}; run `python -m retail_ops demo` first", file=sys.stderr)
            return 2
        (w / "dashboard.html").write_text(render_html(ev), encoding="utf-8")
        (w / "metrics.prom").write_text(render_metrics(ev), encoding="utf-8")
        print(f"wrote {w / 'dashboard.html'} and {w / 'metrics.prom'}")
        return 0
    if a.cmd == "metrics":
        sys.stdout.write(render_metrics(load_events(Path(a.workdir))))
        return 0
    if a.cmd == "healthcheck":
        ok, info = healthcheck(Path(a.workdir) if a.workdir else None, a.max_age_hours)
        print(json.dumps(info))
        return 0 if ok else 1
    from .task import main as task_main
    return task_main()
