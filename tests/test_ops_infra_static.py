"""Static, offline guards over Dockerfile / .dockerignore / Terraform / CI workflow / env example / docs.
These are text checks: they catch regressions in the guarantees we claim, they do not replace terraform validate,
hadolint or checkov (run by CI and by scripts/run_checks.py)."""
import importlib.util
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
TF = {p.name: p.read_text() for p in (ROOT / "platform" / "terraform").glob("*.tf")}
TF_ALL = "\n".join(TF.values())
WF = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
WF_CODE = "\n".join(ln for ln in WF.splitlines() if not ln.lstrip().startswith("#"))  # comments may mention what is NOT done
DOCKERFILE = (ROOT / "Dockerfile").read_text()
DOCKERIGNORE = [ln.strip() for ln in (ROOT / ".dockerignore").read_text().splitlines() if ln.strip() and not ln.startswith("#")]

spec = importlib.util.spec_from_file_location("check_hygiene", ROOT / "scripts" / "check_hygiene.py")
hy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hy)


def test_platform_is_not_a_python_package():
    assert not (ROOT / "platform" / "__init__.py").exists()
    assert (ROOT / "retail_ops" / "__init__.py").exists()


# ---------------------------------------------------------------------------------------------- Dockerfile
def test_dockerfile_is_non_root_pinned_and_minimal():
    assert re.search(r"^USER 10001:10001$", DOCKERFILE, re.M)
    assert DOCKERFILE.index("USER 10001") > DOCKERFILE.rindex("RUN ")  # nothing after USER runs as root-requiring step
    froms = re.findall(r"^FROM (\S+)", DOCKERFILE, re.M)
    assert froms and all(":" in f and not f.endswith(":latest") for f in froms)
    assert "ADD http" not in DOCKERFILE and "curl" not in DOCKERFILE and "sudo" not in DOCKERFILE
    assert "--no-deps" in DOCKERFILE and "requirements-runtime.txt" in DOCKERFILE
    assert 'ENTRYPOINT ["python", "-m", "retail_ops"]' in DOCKERFILE and 'CMD ["task"]' in DOCKERFILE
    assert "EXPOSE" not in DOCKERFILE  # batch job, no listener


@pytest.mark.parametrize("entry", [".git", ".venv", "venv", ".env", ".env.*", "*.pem", "*.key", "*.tfstate", "*.tfvars",
                                   ".review*", "*.duckdb", "*.log", ".demo", ".tools", "tests",
                                   "**/__pycache__", ".terraform", ".aws"])
def test_dockerignore_excludes(entry):
    assert entry in DOCKERIGNORE


def test_dockerfile_copies_only_what_dockerignore_allows():
    for src in re.findall(r"^COPY (?!--from)(\S+)", DOCKERFILE, re.M):
        assert src.rstrip("/") not in DOCKERIGNORE and (ROOT / src).exists(), src
    assert "README.md" in "\n".join(DOCKERIGNORE) and "!README.md" in DOCKERIGNORE  # pyproject needs it


# ---------------------------------------------------------------------------------------------- Terraform
def test_terraform_never_holds_secret_values():
    assert "aws_secretsmanager_secret_version" not in TF_ALL
    assert "secret_string" not in TF_ALL and "secret_binary" not in TF_ALL
    assert not re.search(r'(?i)(password|secret_key|access_key)\s*=\s*"[^"$]', TF_ALL)
    assert not re.search(r"AKIA[0-9A-Z]{16}", TF_ALL)
    assert "valueFrom" in TF["ecs.tf"] and "var.secret_arns" in TF["ecs.tf"]
    assert (ROOT / "platform/terraform/terraform.tfvars").exists() is False


def test_terraform_storage_is_private_encrypted_versioned():
    for needle in ("aws_s3_bucket_public_access_block", "block_public_acls       = true", "restrict_public_buckets = true",
                   'status = "Enabled"', "aws:kms", "enable_key_rotation     = true", "aws:SecureTransport",
                   "BucketOwnerEnforced", "noncurrent_version_expiration"):
        assert needle in TF_ALL, needle
    assert 'acl    = "public' not in TF_ALL and "public-read" not in TF_ALL


def test_terraform_iam_is_scoped_to_exact_resources():
    iam = TF["iam.tf"]
    stars = [m.group(0) for m in re.finditer(r'resources\s*=\s*\["\*"\]', iam)]
    assert len(stars) == 1  # only ecr:GetAuthorizationToken, which has no resource-level permissions
    assert iam.index('actions   = ["ecr:GetAuthorizationToken"]') < iam.index(stars[0]) + 200
    assert not re.search(r'actions\s*=\s*\[[^\]]*"[a-z0-9]+:\*"', iam)  # no service-wide wildcard actions
    assert "AmazonECSTaskExecutionRolePolicy" not in TF_ALL and "AdministratorAccess" not in TF_ALL
    assert "iam:PassedToService" in iam and "ecs:cluster" in iam
    task_policy = iam.split('data "aws_iam_policy_document" "task"')[1].split("aws_iam_role_policy")[0]
    assert "s3:DeleteObject" in task_policy and task_policy.count("s3:DeleteObject") == 1  # only on the lock key
    assert "local.input_prefix" in task_policy and "s3:PutObject" in task_policy
    assert 'resources = ["${aws_s3_bucket.state.arn}/${local.input_prefix}*"]' in task_policy  # inputs read-only


def test_terraform_has_schedule_task_and_failure_alerting():
    assert "aws_scheduler_schedule" in TF_ALL and "aws_ecs_task_definition" in TF_ALL
    assert "aws_cloudwatch_event_rule" in TF_ALL and "aws_sns_topic" in TF_ALL and "aws_cloudwatch_metric_alarm" in TF_ALL
    assert "TaskFailedToStart" in TF_ALL and "dead_letter_config" in TF_ALL
    assert "schedule_enabled" in TF_ALL and re.search(r'variable "schedule_enabled"[^}]*default\s*=\s*false', TF_ALL, re.S)
    assert "readonlyRootFilesystem = true" in TF["ecs.tf"] and '"10001:10001"' in TF["ecs.tf"]
    assert "RETAIL_STATE_BUCKET" in TF["ecs.tf"] and 'command                = ["task"]' in TF["ecs.tf"]
    assert 'default = "X86_64"' in TF["variables.tf"] or "X86_64" in TF["variables.tf"]


def test_terraform_networking_is_user_supplied_and_no_account_data_embedded():
    for res in ("aws_vpc", "aws_subnet", "aws_security_group", "aws_internet_gateway", "aws_nat_gateway"):
        assert f'resource "{res}"' not in TF_ALL
    assert not re.search(r'"\d{12}"', TF_ALL) and not re.search(r"subnet-[0-9a-f]{8}", TF_ALL.replace("subnet-[0-9a-f]", ""))
    assert "subnet_ids" in TF["variables.tf"] and "security_group_ids" in TF["variables.tf"]
    assert (ROOT / "platform/terraform/.terraform.lock.hcl").exists()  # provider selections are committed


def test_task_env_names_in_terraform_match_the_entrypoint():
    from retail_ops.task import TaskConfig
    names = set(re.findall(r'name = "(RETAIL_[A-Z_]+)"', TF["ecs.tf"]))
    src = (ROOT / "retail_ops" / "task.py").read_text()
    assert names and all(n in src for n in names)
    env = {n: "x" for n in names}
    # the exact values Terraform passes must be accepted by the entrypoint's validation
    ok = {"RETAIL_STATE_BUCKET": "b", "RETAIL_STATE_KEY": "state/warehouse.duckdb", "RETAIL_LOCK_KEY": "locks/pipeline.lock",
          "RETAIL_INPUT_PREFIX": "input/", "RETAIL_REPORT_PREFIX": "reports/", "RETAIL_WORK_DIR": "/work",
          "RETAIL_TIMEOUT_SECONDS": "3600", "RETAIL_LOCK_TTL_SECONDS": "4200"}
    assert set(ok) == set(env)
    TaskConfig.from_env(ok)
    for k, v in ok.items():  # and the literal key layout in main.tf equals what we validated above
        if k.endswith(("_KEY", "_PREFIX")):
            assert v in TF["main.tf"]


# ---------------------------------------------------------------------------------------------- CI workflow
def test_workflow_pins_actions_to_shas_has_minimal_permissions_and_no_deploy():
    uses = re.findall(r"uses:\s*(\S+)", WF)
    assert uses
    for u in uses:
        assert re.fullmatch(r"[\w.-]+/[\w.-]+@[0-9a-f]{40}", u), u
    assert re.search(r"^permissions:\n  contents: read$", WF, re.M)
    assert "write" not in WF_CODE.lower()
    for forbidden in ("terraform apply", "terraform plan", "docker push", "aws-actions", "configure-aws-credentials",
                      "id-token", "secrets.", "deploy", "ecr get-login", "environment:"):
        assert forbidden not in WF_CODE, forbidden
    assert WF.count("persist-credentials: false") == WF.count("actions/checkout@")
    for needle in ("ruff check", "mypy", "pytest", "check_hygiene.py", "terraform validate", "tflint", "checkov",
                   "hadolint", "docker build", "terraform fmt -check", "-backend=false"):
        assert needle in WF, needle


def test_workflow_states_it_was_not_executed_on_github():
    assert "not been executed on GitHub" in WF


# ---------------------------------------------------------------------------------------------- secrets hygiene
def test_env_example_is_placeholder_only_and_accepted_by_the_scanner(tmp_path):
    p = ROOT / ".env.example"
    assert p.is_file() and hy.scan_env_example(p.read_text()) == []
    assert hy.scan_secrets(ROOT) == []  # whole repo (incl. .env.example) is clean
    assert not hy.is_suspicious_name(".env.example")


def test_scanner_still_detects_real_looking_secrets_in_example_and_env_files(tmp_path):
    (tmp_path / ".env.example").write_text("SNOWFLAKE_" + "PASSWORD=hunter2hunter2\nAPI_" + "TOKEN=abcd1234efgh5678\nX=1\n")
    hits = hy.scan_secrets(tmp_path)
    assert any("SNOWFLAKE_PASSWORD" in h and "non-placeholder" in h for h in hits), hits
    assert any("API_TOKEN" in h for h in hits)
    (tmp_path / ".env.example").write_text("AWS_KEY=" + "AKIA" + "ABCDEFGHIJKLMNOP\n")
    assert any("AWS access key id" in h for h in hy.scan_secrets(tmp_path))
    for name in (".env", ".env.local", ".env.production", "terraform.tfstate", "prod.tfvars", "x.pem"):
        assert hy.is_suspicious_name(name), name
    for name in (".env.example", ".env.sample", "terraform.tfvars.example", "main.tf", "README.md"):
        assert not hy.is_suspicious_name(name), name


@pytest.mark.parametrize("line", ["TOKEN=<your token>", "DB_PASSWORD=", "SECRET=${SECRET_FROM_VAULT}", "API_KEY=changeme",
                                  "PASSWORD=your-password-here", "SECRET_KEY=REPLACE_ME", "NAME=value"])
def test_env_example_placeholders_accepted(line):
    assert hy.scan_env_example(line) == []


def test_runtime_pins_are_exact_and_consistent_with_the_lock(tmp_path):
    assert hy.check_runtime_pins(ROOT) == []
    (tmp_path / "pyproject.toml").write_text('[project]\nname="x"\nversion="1"\ndependencies=["duckdb>=1"]\n'
                                              '[project.optional-dependencies]\naws=["boto3>=1"]\n')
    (tmp_path / "requirements-lock.txt").write_text("duckdb==1.0.0\n")
    (tmp_path / "requirements-runtime.txt").write_text("duckdb==2.0.0\nurllib3>=2\n")
    problems = "\n".join(hy.check_runtime_pins(tmp_path))
    assert "not an exact pin" in problems and "pinned 2.0.0 but requirements-lock.txt pins 1.0.0" in problems
    assert "boto3" in problems


# ---------------------------------------------------------------------------------------------- docs coherence
def test_docs_state_honest_limitations_and_use_plain_commands_only():
    readme = (ROOT / "platform" / "README.md").read_text()
    low = readme.lower()
    for needle in ("not provisioned", "never applied", "not built", "no docker", "unexecuted on github", "synthetic",
                   "python -m retail_ops demo", "python -m retail_ops dashboard", "127.0.0.1", "if-match"):
        assert needle in low, needle
    assert not (ROOT / "Makefile").exists()  # make is deliberately absent: every documented command is plain Python/shell
    assert not re.search(r"`make \w", readme)
    root_text = (ROOT / "README.md").read_text()
    root = root_text.lower()
    assert "platform/readme.md" in root and "docs/showcase.mp4" in root
    assert "vendored" in root and "retail-data-pipeline" in root
