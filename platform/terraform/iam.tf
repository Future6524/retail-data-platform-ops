data "aws_iam_policy_document" "ecs_tasks_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.account_id]
    }
  }
}

# ---- execution role: used by ECS itself to pull the image, write logs, fetch the referenced secrets
resource "aws_iam_role" "execution" {
  name               = "${var.name_prefix}-execution"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_assume.json
}

data "aws_iam_policy_document" "execution" {
  statement {
    sid       = "WriteOwnLogStreamsOnly"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.task.arn}:*"]
  }

  statement {
    sid       = "EcrAuthToken" # this action does not support resource-level permissions
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"]
  }

  dynamic "statement" {
    for_each = var.ecr_repository_arn == "" ? [] : [1]
    content {
      sid       = "PullFromOneRepository"
      actions   = ["ecr:BatchCheckLayerAvailability", "ecr:GetDownloadUrlForLayer", "ecr:BatchGetImage"]
      resources = [var.ecr_repository_arn]
    }
  }

  dynamic "statement" {
    for_each = length(var.secret_arns) == 0 ? [] : [1]
    content {
      sid       = "ReadExactlyTheListedSecrets"
      actions   = ["secretsmanager:GetSecretValue"]
      resources = [for arn in values(var.secret_arns) : join(":", slice(split(":", arn), 0, 7))]
    }
  }

  dynamic "statement" {
    for_each = length(var.secret_arns) > 0 && var.secrets_kms_key_arn != "" ? [1] : []
    content {
      sid       = "DecryptListedSecrets"
      actions   = ["kms:Decrypt"]
      resources = [var.secrets_kms_key_arn]
    }
  }
}

resource "aws_iam_role_policy" "execution" {
  name   = "execution"
  role   = aws_iam_role.execution.id
  policy = data.aws_iam_policy_document.execution.json
}

# ---- task role: what the pipeline code itself may do. S3 only, exact prefixes, no wildcard actions.
resource "aws_iam_role" "task" {
  name               = "${var.name_prefix}-task"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_assume.json
}

data "aws_iam_policy_document" "task" {
  statement {
    sid       = "ListBucketForSyncAndMissingKey404" # also makes a missing first-run state object a 404 rather than 403
    actions   = ["s3:ListBucket"]
    resources = [aws_s3_bucket.state.arn]
  }

  statement {
    sid       = "ReadInputsOnly"
    actions   = ["s3:GetObject"]
    resources = ["${aws_s3_bucket.state.arn}/${local.input_prefix}*"]
  }

  statement {
    sid       = "ReadWriteWarehouseCheckpoint"
    actions   = ["s3:GetObject", "s3:PutObject"]
    resources = ["${aws_s3_bucket.state.arn}/${local.state_key}"]
  }

  statement {
    sid       = "LeaseLock"
    actions   = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"]
    resources = ["${aws_s3_bucket.state.arn}/${local.lock_key}"]
  }

  statement {
    sid       = "WriteRunReports"
    actions   = ["s3:PutObject"]
    resources = ["${aws_s3_bucket.state.arn}/${local.report_prefix}*"]
  }

  statement {
    sid       = "UseTheBucketKey"
    actions   = ["kms:Decrypt", "kms:GenerateDataKey"]
    resources = [aws_kms_key.main.arn]
  }
}

resource "aws_iam_role_policy" "task" {
  name   = "task"
  role   = aws_iam_role.task.id
  policy = data.aws_iam_policy_document.task.json
}

# ---- scheduler role: may start exactly this task definition in exactly this cluster
data "aws_iam_policy_document" "scheduler_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["scheduler.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.account_id]
    }
  }
}

resource "aws_iam_role" "scheduler" {
  name               = "${var.name_prefix}-scheduler"
  assume_role_policy = data.aws_iam_policy_document.scheduler_assume.json
}

data "aws_iam_policy_document" "scheduler" {
  statement {
    sid       = "RunThisTaskDefinitionInThisCluster"
    actions   = ["ecs:RunTask"]
    resources = ["${aws_ecs_task_definition.main.arn_without_revision}:*"]
    condition {
      test     = "ArnEquals"
      variable = "ecs:cluster"
      values   = [aws_ecs_cluster.main.arn]
    }
  }

  statement {
    sid       = "PassOnlyTheTwoTaskRoles"
    actions   = ["iam:PassRole"]
    resources = [aws_iam_role.execution.arn, aws_iam_role.task.arn]
    condition {
      test     = "StringEquals"
      variable = "iam:PassedToService"
      values   = ["ecs-tasks.amazonaws.com"]
    }
  }

  statement {
    sid       = "DeadLetterFailedInvocations"
    actions   = ["sqs:SendMessage"]
    resources = [aws_sqs_queue.scheduler_dlq.arn]
  }
}

resource "aws_iam_role_policy" "scheduler" {
  name   = "scheduler"
  role   = aws_iam_role.scheduler.id
  policy = data.aws_iam_policy_document.scheduler.json
}
