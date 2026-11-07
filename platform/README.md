# Platform / DevOps layer (SYNTHETIC data, local-first)

CI, container, AWS Terraform, and a **local** monitoring demo around a small vendored copy of the retail pipeline
(`pipeline/`, `data/`; see [`../docs/SPLIT.md`](../docs/SPLIT.md)). Everything processes **synthetic** data only.

## Status: what is and is not verified

| Piece | State | Evidence of what was actually run |
|---|---|---|
| Monitoring demo, dashboard, metrics, local alerts | **Runs and is tested locally, no Docker** | `python -m retail_ops demo`, pytest (`tests/test_ops_*.py`) |
| Task entrypoint + S3 checkpoint/lock logic | Unit/integration tested against an **in-memory S3 mock** | `tests/test_ops_s3state_task.py`. **Never run against real S3.** |
| Terraform (`platform/terraform`) | `fmt`, `init -backend=false`, `validate`, `tflint`, `checkov` are the intended offline checks (see [`../EVIDENCE.md`](../EVIDENCE.md) for what was actually run) | **Not provisioned. `plan`/`apply` never applied or run**, so IAM, KMS-policy and event-pattern behaviour is unproven on AWS |
| Dockerfile | Linted with **hadolint** only | **Not built, not run**: no Docker on the authoring machine |
| `.github/workflows/ci.yml` | Linted with `actionlint`; commands run locally one by one | **Unexecuted on GitHub** (no remote). Job wiring is unproven |

Nothing here sends an alert outside the machine, pushes an image, deploys, or uses cloud credentials. No experience, users,
traffic or uptime numbers are claimed: there are none.

## One-command local demo (no Docker, no network)

```bash
python -m retail_ops demo                     # workdir .demo/monitoring
python -m retail_ops dashboard                # http://127.0.0.1:8765/  (Ctrl-C to stop)
```

Use `.venv/bin/python` instead of `python` if you did not activate the venv. `scripts/run_checks.py` runs every Python step with
the interpreter that launched it (so a fresh venv checks itself). The demo runs the real pipeline four times:

```text
1/4 first load (success)
  run-001  [SUCCESS]  loaded=3  (demo: batch1 only)
2/4 load batch 2 (success)
  run-002  [SUCCESS]  loaded=3, skipped_checkpointed=3  (demo: batch1+batch2)
3/4 SYNTHETIC FAULT: commits of orders_batch3.jsonl are made to fail (pipeline retries 3x, then reports failure)
  run-003  [FAILED ]  failed=1, loaded=2, rejected=1, skipped_checkpointed=6  (demo: all batches + synthetic fault)
      >>> ALERT FIRED    pipeline_run_failed  delivery=local-file-only  [SYNTHETIC FAULT]
4/4 recovery run without the fault
  run-004  [SUCCESS]  loaded=1, skipped_checkpointed=9  (demo: recovery)
      >>> ALERT RESOLVED pipeline_run_failed  delivery=local-file-only
 RESULT: demo invariants held (runs 1-2 succeeded, run 3 failed with the SYNTHETIC fault and the local alert FIRED; run 4 succeeded and the alert RESOLVED)
```

* The failure is a **deliberately injected synthetic fault** (`pipeline/faults.py` makes one file's commit raise), labelled
  `SYNTHETIC injected commit fault ...; NOT a real outage` in the logs, alert and dashboard.
* The "alert" is **local only**: one record appended to `logs/alerts.jsonl` (`"delivery": "local-file-only"`). There is no code
  path that e-mails, posts a webhook or pages anyone.
* Repeatable: re-running replaces the previous demo directory (only if it carries the `.retail-ops-demo` marker the demo
  wrote itself; an empty existing directory is fine; it refuses to touch any other non-empty directory). The result is verified from
  the recorded logs: it requires the exact alert transitions and successful first/recovery runs, not just "no alert is active". Variant `python -m retail_ops demo --leave-firing`
  skips the recovery run so the dashboard shows an **active** alert.
* Outputs in `.demo/monitoring/`: `logs/runs.jsonl` (structured JSON events), `logs/alerts.jsonl`, `metrics.prom`
  (Prometheus text format), `dashboard.html` (static snapshot, open it in a browser, no server needed).
* The live dashboard binds **127.0.0.1** by default and is read-only (GET/HEAD). Binding anything else needs
  `--allow-non-loopback` (it has no authentication). Routes: `/` HTML, `/metrics`, `/api/runs`, `/api/alerts`, `/healthz`.
  Static HTML (`python -m retail_ops render`) and `python -m retail_ops metrics` work without any server.

## Showcase command (progress bar, summary table, openable results)

It prints a real progress bar (it advances once per file actually processed), a summary table and the command that opens the
results. It neither opens a browser nor starts a server by itself.

```bash
python -m retail_ops showcase                 # workdir .demo/ops-showcase
python -m webbrowser -t file:///ABSOLUTE/PATH/.demo/ops-showcase/dashboard.html     # exact command is printed; opens the static page
# optional live view, separate command, loopback only, stop with Ctrl-C:
python -m retail_ops dashboard --workdir .demo/ops-showcase --theme light
python -m webbrowser -t http://127.0.0.1:8765/
```

* Terminal part: 29 files across 4 runs (SYNTHETIC data) -> bar -> table with run, files by status, rows, seconds, result and
  the local alert transition (`FIRED` at run-003, `RESOLVED` at run-004). The command exits 1 if the recorded logs do not prove it.
* Results page (light mode, bright orange header rows, no scripts): summary tiles, runs, alerts and a **File results** table with
  17 columns. Scroll **down** the file rows and **right** across the count columns inside each table's scroll area (sticky header).

## Plain commands for setup and checks (`make` is not used)

Run from the repository root. `PY` = `.venv/bin/python` (or `python` inside an activated venv).

| Task | Command |
|---|---|
| install | `python3 -m venv .venv && .venv/bin/python -m pip install -r requirements-lock.txt && .venv/bin/python -m pip install --no-deps -e .` |
| tests | `.venv/bin/python -m pytest -q` |
| hygiene | `.venv/bin/python scripts/check_hygiene.py --installed` |
| lint / types | `.venv/bin/python -m ruff check .` / `.venv/bin/python -m mypy` |
| download pinned tools | `.venv/bin/python scripts/install_tools.py` (then, for checkov: `python3 -m venv .tools/awsvenv && .tools/awsvenv/bin/pip install checkov==3.2.459`) |
| terraform | `T=.tools/bin/terraform; $T -chdir=platform/terraform fmt -check -recursive && $T -chdir=platform/terraform init -backend=false -input=false && $T -chdir=platform/terraform validate` |
| tflint | `cd platform/terraform && ../../.tools/bin/tflint --init && ../../.tools/bin/tflint --format compact` |
| checkov | `.tools/awsvenv/bin/checkov -d platform/terraform --framework terraform --compact --quiet` |
| hadolint / actionlint | `.tools/bin/hadolint Dockerfile` / `.tools/bin/actionlint` |
| monitoring demo | `.venv/bin/python -m retail_ops demo --workdir .demo/monitoring` |
| everything | `.venv/bin/python scripts/run_checks.py` (a missing tool is reported **SKIPPED**, never passed; `--require-tools` fails on a skip; `RETAIL_TOOLS_DIR` points at a tools directory elsewhere) |

Tools are downloaded by `scripts/install_tools.py` into the git-ignored `.tools/` with the SHA-256 pinned in the script
(integrity against the pin; no publisher-signature verification). Pinned: terraform 1.17.1, tflint 0.65.0 (+ AWS ruleset 0.40.0),
hadolint 2.15.1, actionlint 1.7.12, checkov 3.2.459 (pip).

## Packaging

`platform/` is **not** a Python package (a top-level `platform` package would shadow the stdlib module). Platform code lives in
the importable package **`retail_ops/`** (installed with the project; `python -m retail_ops ...`):

| Module | Role |
|---|---|
| `events.py` | JSON-lines event schema v1 + validator + emitter |
| `instrument.py` | `execute_run`: runs the real pipeline and emits `run_started`, `file_result`, `run_finished` |
| `alerts.py` | local-only alert rule `pipeline_run_failed` (fire once, stay firing, resolve on success) |
| `metrics.py` | Prometheus text rendered by replaying the event log |
| `dashboard.py` | static HTML + loopback read-only HTTP server |
| `demo.py`, `cli.py` | the demo, `render`, `metrics`, `healthcheck` |
| `s3state.py`, `task.py` | AWS task: versioned-S3 checkpoint, lease lock, entrypoint |

**Event schema** (every line): `schema=1, ts (UTC), event, service, run_id, synthetic_data=true`; `run_finished` adds
`ok, outcome ("success"/"failure"), duration_seconds, files{status:n}, totals{...}, error`; failed demo runs add `synthetic_fault`.
**Metrics**: `retail_pipeline_runs_total{status}`, `_files_total{status}`, `_rows_total{kind}`, `_alerts_fired_total`,
`_alerts_firing{alert}`, `_last_run_success`, `_last_run_duration_seconds`, `_last_run_timestamp_seconds`,
`_last_success_timestamp_seconds`. (Counters are derived from the log file, so they only reset if the log is deleted.)
**Healthcheck**: `python -m retail_ops healthcheck [--workdir D --max-age-hours N]` exits 0 only if DuckDB works and (with
`--workdir`) the last recorded run succeeded and is fresh. There is deliberately no Docker `HEALTHCHECK`: the image is a batch job.

## AWS design (`platform/terraform`, offline-validated only)

```text
EventBridge Scheduler ──RunTask──► ECS Fargate task (your image, non-root, read-only rootfs, /work scratch)
   │ (DLQ: SQS)                          │ 1. lease lock   s3://BUCKET/locks/pipeline.lock   (If-None-Match:*)
   │                                     │ 2. restore      s3://BUCKET/state/warehouse.duckdb (remember ETag)
   │                                     │ 3. read inputs  s3://BUCKET/input/*               (read-only)
   │                                     │ 4. run pipeline (same code as local), JSON logs -> CloudWatch Logs
   │                                     │ 5. commit       PUT state with If-Match:<ETag>     (else 412 => refuse)
   │                                     └ 6. report       s3://BUCKET/reports/<run_id>.json, release lock
   ▼
CloudWatch metric filters (outcome=failure/success) ─► alarms ─┐
EventBridge rule: task stopped non-zero / TaskFailedToStart ───┼─► SNS topic (KMS) ─► optional e-mail subscription
Alarm: no successful run for N hours · DLQ not empty ──────────┘
```

* **State is not silently lost.** Fargate disk is ephemeral, so the DuckDB file is checkpointed to a **versioned** S3 object
  after every run that changed anything. Old versions are kept `noncurrent_version_retention_days` (30) for rollback.
* **Concurrency.** (a) a lease-lock object (conditional create; expired leases are taken over with `If-Match`; released only if
  still ours) avoids wasted parallel work; (b) the real guarantee is the **conditional commit**: the state is replaced only if its
  ETag is unchanged since we read it. If another writer got there first the run **fails (exit 3) without overwriting**; the
  next run starts from the newer state and re-processes (merges are idempotent, per-file checkpoints). Cost: that run's work is redone.
* **Partial failure.** If some files fail, the files that succeeded are committed, the run is marked failed (exit 1, alert) and the
  failed files are retried by the next run. A timeout watchdog (`RETAIL_TIMEOUT_SECONDS`) hard-exits 5. It cannot know whether the single conditional state PUT had already
  finished, so the exit-5 event says the commit outcome is **unknown (possibly committed)**; no atomic-timeout guarantee is claimed.
  The next run re-reads whatever state exists and its per-file checkpoints settle it (idempotent). The lease lock is not released by
  a hard exit; it expires after its TTL.
* **Secrets.** The default DuckDB pipeline needs none. Optional `secret_arns = { ENV_NAME = "<secret ARN>" }` injects existing
  Secrets Manager secrets by **reference** (ECS `valueFrom`); you create the secret *values* outside Terraform, so no value is in code
  or state. The execution role gets `GetSecretValue` on exactly those ARNs. `.env.example` holds placeholders only.
* **IAM (least privilege, exact resources).** Task role: `ListBucket` on the bucket; `GetObject` on `input/*`; `Get/PutObject` on the
  one state key; `Get/Put/DeleteObject` on the one lock key; `PutObject` on `reports/*`; KMS use of the one key. Execution role: write
  its own log streams, pull from one ECR repository (if `ecr_repository_arn` set), listed secrets. Scheduler role: `RunTask` for this
  task definition in this cluster, `PassRole` for the two task roles only, `SendMessage` to the DLQ. The only `"*"` resource is
  `ecr:GetAuthorizationToken`, which AWS does not allow to be scoped.
* **Storage.** Private bucket: all public access blocked, owner-enforced, SSE-KMS (rotating CMK), TLS-only and no-unencrypted-PUT policy.
* **Networking is yours.** No VPC/subnet/security group is created. Supply `subnet_ids` and `security_group_ids` (no inbound rules;
  egress 443 to S3, KMS, CloudWatch Logs, ECR and Secrets Manager if used, via NAT or VPC endpoints). `assign_public_ip` defaults to false.
* **Schedule is created DISABLED** (`schedule_enabled = false`) so nothing runs until you opt in.

Use it later (documentation only; these commands were **not** run, they need your AWS account):

```bash
cd platform/terraform
cp terraform.tfvars.example terraform.tfvars        # replace every REPLACE_* placeholder; file is git-ignored
terraform init && terraform plan && terraform apply   # you choose the backend; none is configured here
aws s3 cp my_batches/ s3://<state_bucket output>/input/ --recursive   # then enable the schedule
```

`checkov` is configured with 7 documented, inline `checkov:skip` justifications (key-policy `*` resources, no S3 access-log bucket, no S3
event notifications, no cross-region replication, no CMK for the schedule payload).

## Container

`Dockerfile`: multi-stage, `python:3.13.5-slim-bookworm` (tag pin; **add a digest pin** when you build), pinned
`requirements-runtime.txt`, UID/GID 10001, no shell login, no listening port, `ENTRYPOINT python -m retail_ops` with default `task`.
`.dockerignore` excludes git data, virtualenvs, logs, state files, `.env*`, keys, tfstate/tfvars, review artifacts, tests.
If you have Docker:

```bash
docker build -t retail-pipeline:local .
docker run --rm --read-only --network none --cap-drop ALL retail-pipeline:local healthcheck
docker run --rm --read-only --network none --cap-drop ALL --tmpfs /work:rw,uid=10001,gid=10001 retail-pipeline:local demo --workdir /work/monitoring
```

None of that was executed here.

## CI (`.github/workflows/ci.yml`, unexecuted on GitHub)

Jobs: `python` (ruff, mypy, full pytest, hygiene, monitoring demo), `terraform` (fmt, `init -backend=false`, validate, tflint, checkov),
`dockerfile-lint` (hadolint, checksum-verified), `container-smoke` (docker build + run locally on the runner; **no push**).
Actions are pinned to commit SHAs; `permissions: contents: read`; no secrets, no cloud credentials, no deploy, no `apply`.

## Known limitations

* AWS resources are **not provisioned**; Terraform has never been planned or applied; S3 conditional-write behaviour is modelled by a mock.
* The container image has **not been built or run**; only hadolint-linted. No vulnerability scan of the image or base image.
* The CI workflow has **never executed** on GitHub; the container-smoke job in particular is unproven.
* Task configuration is validated before any filesystem or S3 work: `RETAIL_RUN_ID` must match `[A-Za-z0-9][A-Za-z0-9._-]{0,63}`
  (no `..`, separators, control characters; it names the scratch directory, the report key and the lock owner), state/lock keys must
  differ and must not live under the input/report prefixes, and the input and report prefixes must not overlap. Violations exit 2.
  The task only ever deletes its own `RETAIL_WORK_DIR/<run id>` entry (a symlink there is unlinked, never followed).
* Lease lock has no heartbeat (TTL = timeout + 10 min); a task that stalls past TTL could overlap with a successor, and the
  conditional commit then makes exactly one of them win.
* DuckDB checkpoint is the whole database file: fine for demo-sized data, wasteful at scale.
* `data/generator.py` is exempt from mypy (heterogeneous synthetic record lists; annotating it is future work). ruff style rules
  that would churn the code (line length etc.) are ignored; correctness and bandit rules are on.
* Dashboard is single-user/local: no auth, no TLS, reads the whole log on each request.
* Local alerts are files, not paging. The AWS path alerts through SNS e-mail only if you subscribe and confirm.
