#!/usr/bin/env python3
"""Install the pinned PUBLIC lint/IaC tools into .tools/bin (git-ignored). Stdlib only; no credentials, no cloud.

    python scripts/install_tools.py [--dest .tools] [--only terraform hadolint ...]

Each archive is downloaded into its own empty directory, its SHA-256 is compared with the value pinned below
(taken from the publisher's own checksum file / release digest when the pin was written), and only the one expected
executable is copied out. A mismatch aborts. This verifies integrity against the pin; it does not verify a publisher
signature (GPG/cosign), which is a known limitation.
"""
from __future__ import annotations

import argparse
import hashlib
import shutil
import sys
import tarfile
import tempfile
import urllib.request
import zipfile
from pathlib import Path
from typing import Any

TOOLS: dict[str, dict[str, Any]] = {
    "terraform": dict(
        url="https://releases.hashicorp.com/terraform/1.17.1/terraform_1.17.1_linux_amd64.zip",
        sha256="4638628058e71a55212b1c563712eac03b902128f40740f3dc8174df12135061", member="terraform"),
    "tflint": dict(
        url="https://github.com/terraform-linters/tflint/releases/download/v0.65.0/tflint_linux_amd64.zip",
        sha256="d4c545ed2e29d6338604b586f34f544f380f70f243aa72f7b484713835eaec61", member="tflint"),
    "hadolint": dict(
        url="https://github.com/hadolint/hadolint/releases/download/v2.15.1/hadolint-linux-x86_64",
        sha256="c7187db94eeeeca956519a6af171adc31453941a1e777961f6e680f697c8c507", member=None),
    "actionlint": dict(
        url="https://github.com/rhysd/actionlint/releases/download/v1.7.12/actionlint_1.7.12_linux_amd64.tar.gz",
        sha256="8aca8db96f1b94770f1b0d72b6dddcb1ebb8123cb3712530b08cc387b349a3d8", member="actionlint"),
}


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def install(name: str, spec: dict[str, Any], dest: Path) -> Path:
    bin_dir = dest / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=dest) as tmp:  # fresh empty directory per download
        work = Path(tmp)
        archive = work / "download"
        with urllib.request.urlopen(spec["url"], timeout=120) as resp, archive.open("wb") as out:  # noqa: S310
            shutil.copyfileobj(resp, out)
        got = sha256(archive)
        if got != spec["sha256"]:
            raise SystemExit(f"{name}: CHECKSUM MISMATCH (expected {spec['sha256']}, got {got}); nothing installed")
        target = bin_dir / name
        if spec["member"] is None:
            shutil.copyfile(archive, target)
        elif spec["url"].endswith(".zip"):
            with zipfile.ZipFile(archive) as z, z.open(spec["member"]) as src, target.open("wb") as out:
                shutil.copyfileobj(src, out)
        else:
            with tarfile.open(archive) as t:
                member = t.extractfile(spec["member"])
                if member is None:
                    raise SystemExit(f"{name}: {spec['member']} not found in archive")
                with member, target.open("wb") as out:
                    shutil.copyfileobj(member, out)
        target.chmod(0o755)
    print(f"{name}: sha256 verified, installed {target}")
    return target


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--dest", type=Path, default=Path(".tools"))
    ap.add_argument("--only", nargs="*", choices=sorted(TOOLS))
    args = ap.parse_args(argv)
    for name in args.only or TOOLS:
        install(name, TOOLS[name], args.dest)
    print("checkov is a Python package: python -m venv .tools/awsvenv && .tools/awsvenv/bin/pip install checkov==3.2.459")
    return 0


if __name__ == "__main__":
    sys.exit(main())
