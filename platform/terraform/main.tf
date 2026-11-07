locals {
  account_id     = data.aws_caller_identity.current.account_id
  partition      = data.aws_partition.current.partition
  region         = data.aws_region.current.name
  bucket_name    = "${var.name_prefix}-state-${local.account_id}-${local.region}"
  log_group_name = "/ecs/${var.name_prefix}"
  log_group_arn  = "arn:${local.partition}:logs:${local.region}:${local.account_id}:log-group:${local.log_group_name}"

  # S3 key layout (the task entrypoint reads the same values from its environment)
  state_key     = "state/warehouse.duckdb"
  lock_key      = "locks/pipeline.lock"
  input_prefix  = "input/"
  report_prefix = "reports/"
}

# ---------------------------------------------------------------- encryption key (S3, logs, SNS, queue)
data "aws_iam_policy_document" "kms" {
  # checkov:skip=CKV_AWS_356:In a KMS KEY policy "*" means "this key" (a key policy cannot name another resource).
  # checkov:skip=CKV_AWS_111:Same: key-policy resource "*" is this key; the principals and conditions are what constrain it.
  # checkov:skip=CKV_AWS_109:Same: the account-root statement is the standard way to delegate key admin to IAM policies.
  statement {
    sid       = "AccountAdminViaIam"
    actions   = ["kms:*"]
    resources = ["*"]
    principals {
      type        = "AWS"
      identifiers = ["arn:${local.partition}:iam::${local.account_id}:root"]
    }
  }

  statement {
    sid       = "CloudWatchLogsForThisLogGroupOnly"
    actions   = ["kms:Encrypt*", "kms:Decrypt*", "kms:ReEncrypt*", "kms:GenerateDataKey*", "kms:Describe*"]
    resources = ["*"]
    principals {
      type        = "Service"
      identifiers = ["logs.${local.region}.amazonaws.com"]
    }
    condition {
      test     = "ArnEquals"
      variable = "kms:EncryptionContext:aws:logs:arn"
      values   = [local.log_group_arn]
    }
  }

  statement {
    sid       = "AlarmsAndEventsPublishToEncryptedTopic"
    actions   = ["kms:Decrypt", "kms:GenerateDataKey*"]
    resources = ["*"]
    principals {
      type        = "Service"
      identifiers = ["cloudwatch.amazonaws.com", "events.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.account_id]
    }
  }
}

resource "aws_kms_key" "main" {
  description             = "${var.name_prefix}: state bucket, logs, alerts"
  enable_key_rotation     = true
  deletion_window_in_days = 14
  policy                  = data.aws_iam_policy_document.kms.json
}

resource "aws_kms_alias" "main" {
  name          = "alias/${var.name_prefix}"
  target_key_id = aws_kms_key.main.key_id
}

# ---------------------------------------------------------------- private, versioned, encrypted bucket
resource "aws_s3_bucket" "state" {
  # checkov:skip=CKV_AWS_18:Access-log bucket not provisioned to keep this demo small; add one for production audit needs.
  # checkov:skip=CKV2_AWS_62:No event consumers exist; nothing subscribes to bucket events.
  # checkov:skip=CKV_AWS_144:Cross-region replication is out of scope for a single-region synthetic-data demo; versioning gives rollback.
  bucket = local.bucket_name
}

resource "aws_s3_bucket_public_access_block" "state" {
  bucket                  = aws_s3_bucket.state.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_ownership_controls" "state" {
  bucket = aws_s3_bucket.state.id
  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_versioning" "state" {
  bucket = aws_s3_bucket.state.id
  versioning_configuration {
    status = "Enabled" # every committed warehouse checkpoint stays recoverable
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "state" {
  bucket = aws_s3_bucket.state.id
  rule {
    bucket_key_enabled = true
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = aws_kms_key.main.arn
    }
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "state" {
  bucket = aws_s3_bucket.state.id

  rule {
    id     = "expire-old-versions-and-multiparts"
    status = "Enabled"
    filter {}
    noncurrent_version_expiration {
      noncurrent_days = var.noncurrent_version_retention_days
    }
    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }

  depends_on = [aws_s3_bucket_versioning.state]
}

data "aws_iam_policy_document" "bucket" {
  statement {
    sid       = "DenyInsecureTransport"
    effect    = "Deny"
    actions   = ["s3:*"]
    resources = [aws_s3_bucket.state.arn, "${aws_s3_bucket.state.arn}/*"]
    principals {
      type        = "*"
      identifiers = ["*"]
    }
    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }

  statement {
    sid       = "DenyUnencryptedOrWrongKeyUploads"
    effect    = "Deny"
    actions   = ["s3:PutObject"]
    resources = ["${aws_s3_bucket.state.arn}/*"]
    principals {
      type        = "*"
      identifiers = ["*"]
    }
    condition {
      test     = "StringNotEqualsIfExists"
      variable = "s3:x-amz-server-side-encryption"
      values   = ["aws:kms"]
    }
  }
}

resource "aws_s3_bucket_policy" "state" {
  bucket = aws_s3_bucket.state.id
  policy = data.aws_iam_policy_document.bucket.json

  depends_on = [aws_s3_bucket_public_access_block.state]
}

# ---------------------------------------------------------------- logs
resource "aws_cloudwatch_log_group" "task" {
  name              = local.log_group_name
  retention_in_days = var.log_retention_days
  kms_key_id        = aws_kms_key.main.arn
}
