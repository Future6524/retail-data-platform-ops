"""Ops showcase command: real progress, honest tables, light-mode orange-header dashboard page,
no browser, no server, no invented numbers."""
import io
import re
import threading
import webbrowser
from html.parser import HTMLParser
from pathlib import Path

import pytest

import retail_ops.dashboard as dash_mod
from pipeline.ui import ORANGE
from retail_ops.alerts import LocalAlertManager
from retail_ops.cli import main as p2_main
from retail_ops.events import read_events
from retail_ops.showcase import run_showcase as p2_showcase

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def no_browser_no_server(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("a showcase must never open a browser or start a server")
    for name in ("open", "open_new", "open_new_tab", "get"):
        monkeypatch.setattr(webbrowser, name, boom)
    monkeypatch.setattr(dash_mod, "make_server", boom)


class Tables(HTMLParser):
    """Collects every <table> as {id, headers, rows} plus facts about the page."""

    def __init__(self):
        super().__init__()
        self.tables, self._t, self._cell, self._row, self.tags = [], None, None, None, set()

    def handle_starttag(self, tag, attrs):
        self.tags.add(tag)
        if tag == "table":
            self._t = {"id": dict(attrs).get("id"), "headers": [], "rows": []}
        elif tag in ("th", "td"):
            self._cell = ""
        elif tag == "tr":
            self._row = []

    def handle_data(self, data):
        if self._cell is not None:
            self._cell += data

    def handle_endtag(self, tag):
        if tag in ("th", "td") and self._t is not None:
            (self._t["headers"] if tag == "th" else self._row).append(self._cell)
            self._cell = None
        elif tag == "tr" and self._row:
            self._t["rows"].append(self._row)
            self._row = None
        elif tag == "table":
            self.tables.append(self._t)
            self._t = None


def parse(page):
    p = Tables()
    p.feed(page)
    return {t["id"]: t for t in p.tables}, p.tags


def is_bright_orange(hex_color):
    r, g, b = (int(hex_color[i:i + 2], 16) for i in (1, 3, 5))
    return r >= 0xF0 and 0x70 <= g <= 0xA0 and b <= 0x30


def assert_light_orange_page(page):
    assert is_bright_orange(ORANGE) and f"background:{ORANGE}" in page          # bright orange header row
    assert "prefers-color-scheme:dark" not in page and "color-scheme:light" in page.replace(" ", "")  # light mode
    assert "<script" not in page.lower() and "http://" not in page and "https://" not in page       # offline, static
    assert "overflow:auto" in page and "position:sticky" in page                                     # scroll down + right
    assert "SYNTHETIC" in page


class UTF8(io.StringIO):
    encoding = "utf-8"


@pytest.fixture(scope="module")
def p2(tmp_path_factory):
    d = tmp_path_factory.mktemp("p2") / "show"
    out = UTF8()
    threads_before = threading.active_count()
    return d, p2_showcase(d, out), out.getvalue(), threads_before


def test_p2_exit_progress_table_and_alert_transitions(p2):
    d, code, text, _ = p2
    assert code == 0 and "SYNTHETIC" in text and "LOCAL-ONLY" in text
    events = read_events(d / "logs" / "runs.jsonl")
    nfiles = sum(e["event"] == "file_result" for e in events)
    assert f"100% {nfiles}/{nfiles}" in [ln for ln in text.splitlines() if ln.startswith("[")][-1]
    lines = {m.group(1): [c.strip() for c in m.group(0).strip("│").split("│")]
             for m in (re.match(r"│ (run-\d+)\s+│.*", ln) for ln in text.splitlines()) if m}
    assert set(lines) == {"run-001", "run-002", "run-003", "run-004"}
    assert [lines[r][-2] for r in sorted(lines)] == ["ok", "ok", "FAILED (fault)", "ok"]
    assert [lines[r][-1] for r in sorted(lines)] == ["-", "-", "FIRED", "RESOLVED"]
    for r, fin in {e["run_id"]: e for e in events if e["event"] == "run_finished"}.items():  # numbers == recorded events
        assert int(lines[r][2]) == sum(fin["files"].values()) and int(lines[r][7]) == fin["totals"]["rows_inserted"]
    assert "FIRED pipeline_run_failed at run-003" in text and "RESOLVED pipeline_run_failed at run-004" in text
    fired = [a for a in read_events(d / "logs" / "alerts.jsonl")]
    assert [a["event"] for a in fired] == ["alert_fired", "alert_resolved"] and fired[0]["delivery"] == "local-file-only"


def test_p2_dashboard_page_is_light_orange_scrollable_and_complete(p2):
    d, _, text, _ = p2
    page = (d / "dashboard.html").read_text(encoding="utf-8")
    assert_light_orange_page(page)
    tables, _ = parse(page)
    ft = tables["file-results"]
    events = read_events(d / "logs" / "runs.jsonl")
    assert len(ft["rows"]) == sum(e["event"] == "file_result" for e in events) >= 25 and len(ft["headers"]) >= 15
    assert all(len(r) == len(ft["headers"]) for r in ft["rows"])
    assert {r[1] for r in ft["rows"]} == {"run-001", "run-002", "run-003", "run-004"}
    assert any(r[4] == "failed" and "SYNTHETIC" in r[-1] for r in ft["rows"])
    assert {"runs", "alerts"} <= set(tables) and page.count("background:#ff8c00") >= 1
    assert "ALERT FIRING" not in page and "SYNTHETIC FAULT" in page
    assert f"python -m webbrowser -t {(d / 'dashboard.html').resolve().as_uri()}" in text
    assert "--theme light" in text and "http://127.0.0.1:8765/" in text


def test_p2_no_server_threads_or_browser(p2):
    assert threading.active_count() == p2[3]


def test_p2_fails_loudly_when_alerts_are_suppressed(tmp_path, monkeypatch):
    monkeypatch.setattr(LocalAlertManager, "evaluate", lambda self, finished: None)
    out = io.StringIO()
    assert p2_showcase(tmp_path / "w", out) == 1
    text = out.getvalue()
    assert "showcase checks FAILED" in text and "alert transitions" in text and "verified from the recorded logs" not in text


def test_p2_leave_firing_variant_shows_active_alert(tmp_path):
    out = io.StringIO()
    assert p2_showcase(tmp_path / "w", out, leave_firing=True) == 0
    assert "firing now: pipeline_run_failed" in out.getvalue()
    assert "ALERT FIRING" in (tmp_path / "w" / "dashboard.html").read_text()


def test_p2_cli_entry_and_light_theme_server_option(tmp_path, capsys):
    assert p2_main(["showcase", "--workdir", str(tmp_path / "w")]) == 0
    assert "python -m webbrowser -t file://" in capsys.readouterr().out
    ev = read_events(tmp_path / "w" / "logs" / "runs.jsonl")
    assert "prefers-color-scheme:dark" in dash_mod.render_html(ev) and "prefers-color-scheme:dark" not in dash_mod.render_html(ev, 0, "light")


def test_dashboard_light_theme_served_over_loopback(tmp_path):
    import urllib.request
    p2_showcase(tmp_path / "w", io.StringIO())
    srv = dash_mod.ThreadingHTTPServer(("127.0.0.1", 0), dash_mod.make_handler(tmp_path / "w", 0, dash_mod.LOOPBACK_NAMES, "light"))
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        body = urllib.request.urlopen(f"http://127.0.0.1:{srv.server_address[1]}/", timeout=5).read().decode()  # noqa: S310
    finally:
        srv.shutdown()
        srv.server_close()
        t.join(5)
    assert not t.is_alive() and "prefers-color-scheme:dark" not in body and "File results" in body


def test_docs_name_the_showcase_command_and_stay_honest():
    readme, plat = (ROOT / "README.md").read_text(), (ROOT / "platform" / "README.md").read_text()
    for needle in ("python -m retail_ops showcase", "python -m webbrowser -t"):
        assert needle in readme + plat, needle
    assert "29 files across 4 runs" in plat   # = the tested numbers above
    assert (ROOT / "docs" / "showcase.mp4").is_file()
