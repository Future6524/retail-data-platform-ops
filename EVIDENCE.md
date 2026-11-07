# Evidence: retail-data-platform-ops

All data is **SYNTHETIC**. This file states what was actually run, by whom, and what was **not** run.

## Who ran what

- **Earlier monorepo work** produced the platform/monitoring/Terraform/CI/Docker material. Its claims are the author's, not independent verification.
- **Coordinator, independent post-split run (2026-11-07):** fresh `.venv` in this tree, via `scripts/run_checks.py`, first without and then with `RETAIL_TOOLS_DIR` pointing at locally installed tools. Verbatim output is saved outside this repo in `split-evidence/ops-post-split-checks.txt` and `split-evidence/ops-with-tools-checks.txt`.

## Post-split results

| Check | Result |
|---|---|
| `ruff check .` | PASS |
| `mypy` | PASS (34 source files) |
| `pytest -q` | PASS: **192 passed** (13.23s; 13.07s in the second run) |
| `check_hygiene.py --installed` | PASS: 66 files, 6 secret patterns, 0 problems |
| `terraform fmt -check -recursive` | PASS |
| `terraform init -backend=false` | PASS (provider hashicorp/aws 5.100.0 from lock file) |
| `terraform validate` | PASS ("configuration is valid") |
| `tflint` | PASS |
| `checkov` (terraform) | PASS: 140 passed, 0 failed, **7 skipped** (skips are explicit suppressions in the code; review them) |
| `hadolint Dockerfile` | PASS |
| `python -m retail_ops demo` | PASS: SYNTHETIC fault -> local alert FIRED -> recovery -> alert RESOLVED (local file only, nothing sent) |
| `actionlint` | **FAIL in that run**, only because the tree had no `.git` yet ("no project was found"); expected to clear after `git init`. That earlier result was not re-verified in that recorded run. |

## Not run / not verified

- **No `terraform plan` or `terraform apply`**; no AWS account or credentials were used. Terraform was validated offline only (`init -backend=false`, `validate`, static scanners). The infrastructure has never been created.
- **No Docker build or run**: the Dockerfile was only linted with hadolint.
- **No Snowflake account.**
- **No real AI/model calls** (not part of this repo).
- **No GitHub Actions execution yet**; workflows are not proven on GitHub runners.
- Alert delivery is local-file only; no email/Slack/PagerDuty path was exercised.
- Static scanners passing is not a security audit.

## Historic context

Deeper reviews happened in the monorepo era (before the split) and are history only. The video and the split are shared with sibling repos.

## Reproduce

```
python -m venv .venv && .venv/bin/pip install -r requirements-lock.txt -e .
.venv/bin/python scripts/run_checks.py     # set RETAIL_TOOLS_DIR to include terraform/tflint/checkov/hadolint/actionlint
```
