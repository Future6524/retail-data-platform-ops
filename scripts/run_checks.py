#!/usr/bin/env python3
"""Run every local check available on this machine (plain Python; no `make` needed).

    python scripts/run_checks.py [--skip NAME ...] [--require-tools]

Python steps run with the interpreter that started this script (``sys.executable``), so a fresh
venv checks itself. Tools are looked up in ``$RETAIL_TOOLS_DIR/bin`` (default ``.tools/bin``, see
scripts/install_tools.py), then on PATH; checkov in ``<tools dir>/awsvenv``. A missing tool is reported as
SKIPPED, never as passed; --require-tools turns a skip into a failure.
Nothing here contacts AWS: terraform init downloads only the public provider; no apply/plan is ever run.
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TOOLS_ROOT = Path(os.environ.get("RETAIL_TOOLS_DIR") or ROOT / ".tools").resolve()
TOOLS = TOOLS_ROOT / "bin"
PY = sys.executable  # always the interpreter that launched this script (never a silently preferred repo venv)


def tool(name: str) -> str | None:
    local = TOOLS / name
    return str(local) if local.exists() else shutil.which(name)


def steps() -> list[tuple[str, list[str] | None, Path, str | None]]:
    """(name, command or None when the tool is missing, cwd, missing-tool hint)"""
    tf, tflint, hado, al = tool("terraform"), tool("tflint"), tool("hadolint"), tool("actionlint")
    chk = TOOLS_ROOT / "awsvenv" / "bin" / "checkov"
    tfdir = ROOT / "platform" / "terraform"
    hint = "python scripts/install_tools.py"
    return [
        ("ruff", [PY, "-m", "ruff", "check", "."], ROOT, None),
        ("mypy", [PY, "-m", "mypy"], ROOT, None),
        ("pytest", [PY, "-m", "pytest", "-q"], ROOT, None),
        ("hygiene", [PY, "scripts/check_hygiene.py", "--installed"], ROOT, None),
        ("terraform-fmt", [tf, "fmt", "-check", "-recursive"] if tf else None, tfdir, hint),
        ("terraform-init", [tf, "init", "-backend=false", "-input=false"] if tf else None, tfdir, hint),
        ("terraform-validate", [tf, "validate"] if tf else None, tfdir, hint),
        ("tflint", [tflint, "--format", "compact"] if tflint else None, tfdir, hint),
        ("checkov", [str(chk), "-d", "platform/terraform", "--framework", "terraform", "--compact", "--quiet"]
         if chk.exists() else None, ROOT, "python -m venv .tools/awsvenv && .tools/awsvenv/bin/pip install checkov==3.2.459"),
        ("hadolint", [hado, "Dockerfile"] if hado else None, ROOT, hint),
        ("actionlint", [al] if al else None, ROOT, hint),
        ("ops-demo", [PY, "-m", "retail_ops", "demo", "--workdir", ".demo/monitoring"], ROOT, None),
    ]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--skip", nargs="*", default=[])
    ap.add_argument("--require-tools", action="store_true")
    args = ap.parse_args(argv)
    plugins = TOOLS_ROOT / "tflint-plugins"
    env = {**os.environ, "TFLINT_PLUGIN_DIR": str(plugins), "PYTHONDONTWRITEBYTECODE": "1"}
    plugins.mkdir(parents=True, exist_ok=True)
    results: list[tuple[str, str]] = []
    for name, cmd, cwd, hint in steps():
        if name in args.skip:
            results.append((name, "SKIPPED (--skip)"))
            continue
        if cmd is None:
            results.append((name, f"SKIPPED (tool missing; install: {hint})"))
            continue
        if name == "tflint":  # plugin download (public GitHub release) into the plugin dir; a no-op when already installed
            subprocess.run([cmd[0], "--init"], cwd=cwd, env=env, check=False, capture_output=True)  # noqa: S603
        print(f"\n=== {name}: {' '.join(Path(c).name if i == 0 else c for i, c in enumerate(cmd))}", flush=True)
        rc = subprocess.run(cmd, cwd=cwd, env=env, check=False).returncode  # noqa: S603
        results.append((name, "PASS" if rc == 0 else f"FAIL (exit {rc})"))
    print("\n==== summary")
    for name, res in results:
        print(f"{name:20s} {res}")
    failed = any(r.startswith("FAIL") or (args.require_tools and r.startswith("SKIPPED (tool")) for _, r in results)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
