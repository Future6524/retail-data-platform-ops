# retail-data-platform-ops (SYNTHETIC data, local-first)

The platform / DevOps layer of a fictional retailer's data stack: a **local monitoring demo** (structured run logs, Prometheus-format
metrics, local-only alerts, dashboard), an **AWS scheduled-task design** (Terraform, S3 checkpoint + lease lock), a hardened
**Dockerfile** and a **GitHub Actions** workflow. All data is produced by a seeded generator; the retailer and every record are fictional.

* **AWS is not provisioned and Terraform was never planned or applied.** Offline `fmt`/`init -backend=false`/`validate`/`tflint`/`checkov` are the only checks (see [`EVIDENCE.md`](EVIDENCE.md)).
* **The container image has not been built or run** (no Docker on the authoring machine); it is linted with hadolint only.
* **The CI workflow has never executed on GitHub** (no remote exists); it was linted with actionlint and its commands run locally.
* The S3 state/lock logic is tested against an **in-memory S3 fake**, never against real S3.
* Nothing here deploys, pushes an image, sends an alert off the machine or uses cloud credentials.

Showcase video (silent screen recording of the terminal run and the dashboard): [`docs/showcase.mp4`](docs/showcase.mp4).

Full details, AWS design and limitations: [`platform/README.md`](platform/README.md). Origin of this repository and of the **vendored**
`pipeline/` + `data/` packages (copies of the `retail-data-pipeline` repository, needed because the monitor instruments the real pipeline): [`docs/SPLIT.md`](docs/SPLIT.md).

## Quick start (plain commands; `make` is not used)

Requirements: Python 3.11+ (tested **only on 3.13.5 / Linux**), `venv`, `pip`, internet once to fetch pinned packages from PyPI.

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-lock.txt
.venv/bin/python -m pip install --no-deps -e .
.venv/bin/python -m pytest -q                                  # offline test suite (S3 is an in-memory fake)
.venv/bin/python -m retail_ops demo --workdir .demo/monitoring # success -> SYNTHETIC fault -> local-only alert -> recovery
.venv/bin/python -m retail_ops showcase                        # progress bar + table + .demo/ops-showcase/dashboard.html
.venv/bin/python -m retail_ops dashboard --workdir .demo/monitoring   # http://127.0.0.1:8765/ (loopback only; Ctrl-C to stop)
.venv/bin/python scripts/check_hygiene.py --installed          # lock pins + secret-pattern scan (not a vulnerability scan)
.venv/bin/python scripts/run_checks.py                         # ruff, mypy, pytest, hygiene, terraform, tflint, checkov, hadolint, actionlint, demo
```

The Terraform / container / workflow linters are not Python packages. `scripts/install_tools.py` downloads pinned, checksum-verified
binaries into the git-ignored `.tools/` (terraform, tflint, hadolint, actionlint); checkov is pip-installed into `.tools/awsvenv`
(commands in [`platform/README.md`](platform/README.md#plain-commands-for-setup-and-checks-make-is-not-used)). `scripts/run_checks.py` reports a missing tool as
**SKIPPED**, never as passed; `--require-tools` turns a skip into a failure, and `RETAIL_TOOLS_DIR` can point at a tools directory kept elsewhere.

Open the printed static page with `python -m webbrowser -t file:///ABSOLUTE/PATH/.demo/ops-showcase/dashboard.html`
(the exact command is printed at the end of `showcase`). Nothing opens a browser or starts a server unless you run a command yourself.

## Layout

| Path | Purpose |
|---|---|
| `retail_ops/` | Monitoring (events, metrics, alerts, dashboard), demo, showcase, AWS task + S3 state |
| `platform/terraform/` | AWS design: ECS Fargate task, EventBridge schedule, versioned/KMS S3, IAM, alarms (validated offline only) |
| `platform/README.md` | Detailed docs (status table, AWS design, container, CI, limitations) |
| `Dockerfile`, `.dockerignore`, `requirements-runtime.txt` | Container image (not built here) |
| `.github/workflows/ci.yml` | CI: python, terraform, dockerfile-lint, container-smoke (never run on GitHub) |
| `pipeline/`, `data/` | **Vendored** copy of the pipeline and synthetic generator (this repo does not own them) |
| `scripts/` | `check_hygiene.py`, `run_checks.py`, `install_tools.py` |
| `tests/` | `test_ops_*.py` (+ in-memory `fake_s3.py`); static infra guards; hygiene tests |
| `docs/` | `showcase.mp4`, `SPLIT.md` |

## Honest limits

No uptime, user, traffic or cost numbers exist or are claimed. The "alert" is a line in a local file. The dashboard has no authentication and
binds loopback only unless `--allow-non-loopback` is passed. Terraform behaviour on AWS (IAM, KMS policies, event patterns, S3 conditional writes)
is unproven. Only Python 3.13.5 on Linux was used.

## License

MIT, see [`LICENSE`](LICENSE). Copyright (c) 2026 Saveliev Dmitry.
