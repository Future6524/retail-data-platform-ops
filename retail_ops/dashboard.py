"""Self-contained local HTML dashboard + tiny read-only HTTP server (stdlib only, loopback by default).

Routes (GET/HEAD only): /  HTML dashboard   /metrics  Prometheus text   /api/runs  /api/alerts  JSON   /healthz
Nothing is served from disk by path, so there is no path-traversal surface; every dynamic value is HTML-escaped.
"""
from __future__ import annotations

import html
import ipaddress
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .alerts import firing_alerts
from .events import Event, read_events
from .metrics import render_metrics

LOOPBACK_NAMES = {"localhost", "127.0.0.1", "::1"}
RUN_LOG = "logs/runs.jsonl"
CSP = "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"


def is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def load_events(workdir: Path) -> list[Event]:
    return read_events(workdir / RUN_LOG)


def summarize_runs(events: list[Event]) -> list[dict[str, Any]]:
    """One row per run (latest last), joining run_finished with its fault label and file results."""
    runs: dict[str, dict[str, Any]] = {}
    for e in events:
        rid = str(e.get("run_id"))
        if e.get("event") == "run_started":
            runs[rid] = {"run_id": rid, "started": e.get("ts"), "trigger": e.get("trigger"), "finished": None,
                         "ok": None, "fault": e.get("synthetic_fault"), "files": {}, "totals": {}, "error": None,
                         "duration": None}
        elif e.get("event") == "run_finished" and rid in runs:
            r = runs[rid]
            r.update(finished=e.get("ts"), ok=bool(e.get("ok")), files=e.get("files") or {},
                     totals=e.get("totals") or {}, error=e.get("error"), duration=e.get("duration_seconds"))
    return list(runs.values())


def _badge(ok: bool | None) -> str:
    if ok is None:
        return '<span class="b run">RUNNING/UNKNOWN</span>'
    return '<span class="b ok">&#10003; SUCCESS</span>' if ok else '<span class="b bad">&#10007; FAILED</span>'


FILE_COUNT_COLS = [("Read", "rows_read"), ("Inserted", "rows_inserted"), ("Updated", "rows_updated"),
                   ("Unchanged", "rows_unchanged"), ("Stale", "rows_stale"), ("Late", "rows_late"),
                   ("Batch dups", "rows_batch_duplicates"), ("Quarantined", "rows_quarantined"),
                   ("Out of window", "rows_out_of_window")]
FILE_HEADERS = ["Time (UTC)", "Run", "File", "Entity", "Status", "Attempts"] + [h for h, _ in FILE_COUNT_COLS] + [
    "Error", "Fault label"]
ORANGE = "#ff8c00"  # bright orange table header rows (dark text on it: contrast > 8:1)


def file_result_rows(events: list[Event]) -> list[list[Any]]:
    """One row per file_result event, in log order (the data behind the 'File results' table)."""
    rows = []
    for e in events:
        if e.get("event") == "file_result":
            c = e.get("counts") or {}
            rows.append([e.get("ts"), e.get("run_id"), e.get("file"), e.get("entity"), e.get("status"),
                         e.get("attempts")] + [c.get(k, 0) for _, k in FILE_COUNT_COLS]
                        + [e.get("error") or "", e.get("synthetic_fault") or ""])
    return rows


def render_html(events: list[Event], refresh_seconds: int = 0, theme: str = "auto") -> str:
    """``theme``: "auto" follows the OS (light/dark); "light" is always light (used by the showcase)."""
    esc = html.escape
    runs = summarize_runs(events)
    alerts = [e for e in events if e.get("event") in ("alert_fired", "alert_resolved")]
    firing = firing_alerts(events)
    n_ok = sum(1 for r in runs if r["ok"])
    n_bad = sum(1 for r in runs if r["ok"] is False)
    rows_in = sum(int(r["totals"].get("rows_inserted", 0)) for r in runs)
    rows_q = sum(int(r["totals"].get("rows_quarantined", 0)) for r in runs)

    def tile(label: str, value: object, cls: str = "") -> str:
        return f'<div class="tile {cls}"><div class="v">{esc(str(value))}</div><div class="l">{esc(label)}</div></div>'

    strip = "".join(f'<span class="sq {"ok" if r["ok"] else "bad" if r["ok"] is False else "run"}" '
                    f'title="{esc(r["run_id"])}"></span>' for r in runs[-40:]) or "no runs yet"
    syn = '<span class="b syn">SYNTHETIC FAULT</span>'

    def run_row(r: dict[str, Any]) -> str:
        files = ", ".join(f"{k}={v}" for k, v in sorted(r["files"].items())) or "-"
        fault = f" {syn} {esc(str(r['fault']))}" if r["fault"] else ""
        return (f'<tr><td>{esc(str(r["finished"] or r["started"]))}</td><td>{esc(r["run_id"])}</td>'
                f'<td>{_badge(r["ok"])}</td><td class="n">{esc(str(r["duration"]))}s</td><td>{esc(files)}</td>'
                f'<td class="n">{esc(str(r["totals"].get("rows_inserted", 0)))}</td>'
                f'<td class="n">{esc(str(r["totals"].get("rows_quarantined", 0)))}</td>'
                f'<td>{esc(str(r["error"] or ""))}{fault}</td></tr>')

    def alert_row(a: Event) -> str:
        state = ('<span class="b bad">FIRING</span>' if a["event"] == "alert_fired"
                 else '<span class="b ok">RESOLVED</span>')
        fault = f" {syn}" if a.get("synthetic_fault") else ""
        return (f'<tr><td>{esc(str(a["ts"]))}</td><td>{esc(str(a["alert"]))}</td><td>{state}</td>'
                f'<td>{esc(str(a["summary"]))}{fault}</td><td>{esc(str(a.get("delivery")))}</td></tr>')

    frows = file_result_rows(events)
    num_from, num_to = 5, 5 + 1 + len(FILE_COUNT_COLS)

    def file_row(r: list[Any]) -> str:
        cells = []
        for i, v in enumerate(r):
            cls = "n" if num_from <= i < num_to else ""
            if i == 4:
                cls = "badc" if v == "failed" else "okc" if v == "loaded" else ""
            cells.append(f'<td class="{cls}">{esc(str(v))}</td>' if cls else f"<td>{esc(str(v))}</td>")
        return "<tr>" + "".join(cells) + "</tr>"

    file_rows = "".join(file_row(r) for r in reversed(frows)) \
        or f'<tr><td colspan="{len(FILE_HEADERS)}">No file results recorded.</td></tr>'
    file_head = "".join(f"<th>{esc(h)}</th>" for h in FILE_HEADERS)
    dark = ":root{color-scheme:light}" if theme == "light" else (
        "@media (prefers-color-scheme:dark){:root{--bg:#14171a;--fg:#e8eaed;--card:#1d2125;--line:#363b41;--ok:#5fd08a;"
        "--bad:#ff8a80;--syn:#ffcc66;--mut:#9aa3ad}}")
    run_rows = "".join(run_row(r) for r in reversed(runs)) \
        or '<tr><td colspan="8">No runs recorded. Run: python -m retail_ops demo</td></tr>'
    alert_rows = "".join(alert_row(a) for a in reversed(alerts)) or '<tr><td colspan="5">No alerts.</td></tr>'
    banner = ("".join(f'<div class="alert">&#9888; ALERT FIRING: {esc(k)} - {esc(str(v.get("summary")))}</div>'
                      for k, v in firing.items())
              or '<div class="alert none">&#10003; No alerts firing</div>')
    refresh = f'<meta http-equiv="refresh" content="{int(refresh_seconds)}">' if refresh_seconds > 0 else ""
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
{refresh}<meta name="color-scheme" content="{"light" if theme == "light" else "light dark"}">
<title>Retail pipeline - local monitoring (SYNTHETIC)</title>
<style>
:root{{--bg:#f6f7f9;--fg:#1b1f24;--card:#fff;--line:#d8dce1;--ok:#0a7a3d;--bad:#b3261e;--syn:#7a4f00;--mut:#5b6470}}
{dark}
body{{margin:0;font:15px/1.45 system-ui,sans-serif;background:var(--bg);color:var(--fg)}}
main{{max-width:1180px;margin:0 auto;padding:20px}} h1{{font-size:22px;margin:0 0 4px}} h2{{font-size:16px;margin:26px 0 8px}}
.sub{{color:var(--mut)}} .tiles{{display:flex;gap:12px;flex-wrap:wrap;margin:14px 0}}
.tile{{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:10px 16px;min-width:130px}}
.tile .v{{font-size:26px;font-weight:700}} .tile .l{{color:var(--mut);font-size:12px;text-transform:uppercase;letter-spacing:.04em}}
.tile.bad .v{{color:var(--bad)}} .tile.ok .v{{color:var(--ok)}}
.alert{{border:2px solid var(--bad);color:var(--bad);border-radius:8px;padding:10px 14px;font-weight:700;margin:8px 0}}
.alert.none{{border-color:var(--ok);color:var(--ok)}}
table{{width:100%;border-collapse:collapse;background:var(--card);border:1px solid var(--line);font-size:13px}}
th,td{{text-align:left;padding:6px 8px;border-bottom:1px solid var(--line);vertical-align:top}} td.n{{text-align:right}}
.b{{display:inline-block;border-radius:4px;padding:1px 6px;font-size:12px;font-weight:700;border:1px solid currentColor}}
.b.ok{{color:var(--ok)}} .b.bad{{color:var(--bad)}} .b.syn{{color:var(--syn)}} .b.run{{color:var(--mut)}}
.sq{{display:inline-block;width:14px;height:14px;margin-right:3px;border-radius:3px;background:var(--mut)}}
.sq.ok{{background:var(--ok)}} .sq.bad{{background:var(--bad)}}
thead th{{position:sticky;top:0;background:{ORANGE};color:#111;border-bottom:2px solid #c96a00;white-space:nowrap}}
.scroll{{overflow:auto;max-height:62vh;border:1px solid var(--line)}} .scroll table{{border:0;min-width:100%}}
.scroll td{{white-space:nowrap}} td.badc{{color:var(--bad);font-weight:700}} td.okc{{color:var(--ok);font-weight:700}}
footer{{margin-top:24px;color:var(--mut);font-size:12px}}
</style></head><body><main>
<h1>Retail pipeline - local monitoring</h1>
<div class="sub">All data is <b>SYNTHETIC</b>. Alerts are <b>local-only</b> (written to a file; nothing is sent anywhere).</div>
{banner}
<div class="tiles">{tile("runs", len(runs))}{tile("successful", n_ok, "ok")}{tile("failed", n_bad, "bad" if n_bad else "")}
{tile("alerts firing", len(firing), "bad" if firing else "ok")}{tile("rows inserted", rows_in)}{tile("rows quarantined", rows_q)}</div>
<h2>Recent runs (oldest to newest)</h2><div>{strip}</div>
<h2>Runs</h2><div class="scroll"><table id="runs"><thead><tr><th>Finished (UTC)</th><th>Run</th><th>Status</th><th>Duration</th><th>Files</th><th>Inserted</th><th>Quarantined</th><th>Error / fault label</th></tr></thead><tbody>{run_rows}</tbody></table></div>
<h2>Alerts</h2><div class="scroll"><table id="alerts"><thead><tr><th>Time (UTC)</th><th>Alert</th><th>State</th><th>Summary</th><th>Delivery</th></tr></thead><tbody>{alert_rows}</tbody></table></div>
<h2>File results ({len(frows)}; newest first; scroll down for rows, right for columns)</h2><div class="scroll"><table id="file-results"><thead><tr>{file_head}</tr></thead><tbody>{file_rows}</tbody></table></div>
<footer>Links: <code>/metrics</code> (Prometheus text) &middot; <code>/api/runs</code> &middot; <code>/api/alerts</code> &middot; <code>/healthz</code></footer>
</main></body></html>
"""


def make_handler(workdir: Path, refresh_seconds: int, allowed_hosts: set[str] | None, theme: str = "auto"):
    class Handler(BaseHTTPRequestHandler):
        server_version = "RetailOps"
        sys_version = ""

        def log_message(self, fmt: str, *args: Any) -> None:  # keep test/demo output quiet
            pass

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", CSP)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _route(self) -> None:
            if allowed_hosts is not None:  # DNS-rebinding guard for the loopback default
                host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]").lower()
                if host not in allowed_hosts:
                    return self._send(403, b"forbidden host\n", "text/plain; charset=utf-8")
            path = self.path.split("?", 1)[0]
            events = load_events(workdir)
            if path == "/":
                self._send(200, render_html(events, refresh_seconds, theme).encode(), "text/html; charset=utf-8")
            elif path == "/metrics":
                self._send(200, render_metrics(events).encode(), "text/plain; version=0.0.4; charset=utf-8")
            elif path == "/api/runs":
                self._send(200, json.dumps(summarize_runs(events)).encode(), "application/json")
            elif path == "/api/alerts":
                a = [e for e in events if str(e.get("event", "")).startswith("alert_")]
                self._send(200, json.dumps({"firing": list(firing_alerts(events)), "events": a}).encode(),
                           "application/json")
            elif path == "/healthz":
                self._send(200, b'{"status":"ok"}', "application/json")
            else:
                self._send(404, b"not found\n", "text/plain; charset=utf-8")

        do_GET = do_HEAD = _route

        def _deny(self) -> None:
            self.send_response(405)
            self.send_header("Allow", "GET, HEAD")
            self.send_header("Content-Length", "0")
            self.end_headers()

        do_POST = do_PUT = do_DELETE = do_PATCH = _deny

    return Handler


def make_server(workdir: Path, host: str = "127.0.0.1", port: int = 8765, *, allow_non_loopback: bool = False,
                refresh_seconds: int = 5, theme: str = "auto") -> ThreadingHTTPServer:
    if not is_loopback(host) and not allow_non_loopback:
        raise ValueError(f"refusing to bind {host!r}: the dashboard is unauthenticated; "
                         "pass --allow-non-loopback only if you understand that")
    allowed = LOOPBACK_NAMES if is_loopback(host) else None
    return ThreadingHTTPServer((host, port), make_handler(workdir, refresh_seconds, allowed, theme))
