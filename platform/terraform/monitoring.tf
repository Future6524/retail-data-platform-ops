# ---------------------------------------------------------------- alert topic (encrypted, TLS-only)
resource "aws_sns_topic" "alerts" {
  name              = "${var.name_prefix}-alerts"
  kms_master_key_id = aws_kms_key.main.arn
}

data "aws_iam_policy_document" "alerts" {
  statement {
    sid       = "OnlyThisAccountsAlarmsAndRulesPublish"
    actions   = ["sns:Publish"]
    resources = [aws_sns_topic.alerts.arn]
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

  statement {
    sid       = "DenyInsecureTransport"
    effect    = "Deny"
    actions   = ["sns:Publish", "sns:Subscribe"]
    resources = [aws_sns_topic.alerts.arn]
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
}

resource "aws_sns_topic_policy" "alerts" {
  arn    = aws_sns_topic.alerts.arn
  policy = data.aws_iam_policy_document.alerts.json
}

resource "aws_sns_topic_subscription" "email" {
  count     = var.alert_email == "" ? 0 : 1
  topic_arn = aws_sns_topic.alerts.arn
  protocol  = "email"
  endpoint  = var.alert_email
}

# ---------------------------------------------------------------- run outcome metrics from the JSON logs
resource "aws_cloudwatch_log_metric_filter" "run_failed" {
  name           = "${var.name_prefix}-run-failed"
  log_group_name = aws_cloudwatch_log_group.task.name
  pattern        = "{ ($.event = \"run_finished\") && ($.outcome = \"failure\") }"

  metric_transformation {
    name          = "RunFailed"
    namespace     = "RetailPipeline"
    value         = "1"
    default_value = "0"
  }
}

resource "aws_cloudwatch_log_metric_filter" "run_succeeded" {
  name           = "${var.name_prefix}-run-succeeded"
  log_group_name = aws_cloudwatch_log_group.task.name
  pattern        = "{ ($.event = \"run_finished\") && ($.outcome = \"success\") }"

  metric_transformation {
    name          = "RunSucceeded"
    namespace     = "RetailPipeline"
    value         = "1"
    default_value = "0"
  }
}

resource "aws_cloudwatch_metric_alarm" "run_failed" {
  alarm_name          = "${var.name_prefix}-run-failed"
  alarm_description   = "A pipeline run logged outcome=failure (file failure, lock/state conflict, crash or timeout)."
  namespace           = "RetailPipeline"
  metric_name         = "RunFailed"
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alerts.arn]
  ok_actions          = [aws_sns_topic.alerts.arn]
}

resource "aws_cloudwatch_metric_alarm" "no_recent_success" {
  alarm_name          = "${var.name_prefix}-no-recent-success"
  alarm_description   = "No successful run was logged for ${var.max_hours_without_success}h (schedule disabled, task cannot start, or runs keep failing)."
  namespace           = "RetailPipeline"
  metric_name         = "RunSucceeded"
  statistic           = "Sum"
  period              = 3600
  evaluation_periods  = var.max_hours_without_success
  threshold           = 1
  comparison_operator = "LessThanThreshold"
  treat_missing_data  = "breaching"
  alarm_actions       = [aws_sns_topic.alerts.arn]
  ok_actions          = [aws_sns_topic.alerts.arn]
}

resource "aws_cloudwatch_metric_alarm" "scheduler_dlq" {
  alarm_name          = "${var.name_prefix}-scheduler-could-not-start-task"
  alarm_description   = "EventBridge Scheduler could not invoke RunTask (message landed in the DLQ)."
  namespace           = "AWS/SQS"
  metric_name         = "ApproximateNumberOfMessagesVisible"
  dimensions          = { QueueName = aws_sqs_queue.scheduler_dlq.name }
  statistic           = "Maximum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alerts.arn]
}

# ---------------------------------------------------------------- ECS-level failures (never reached our logs)
resource "aws_cloudwatch_event_rule" "task_failed" {
  name        = "${var.name_prefix}-task-failed"
  description = "Pipeline task stopped with a non-zero exit code or failed to start (e.g. image pull, secret fetch)."

  event_pattern = jsonencode({
    source        = ["aws.ecs"]
    "detail-type" = ["ECS Task State Change"]
    detail = {
      clusterArn = [aws_ecs_cluster.main.arn]
      lastStatus = ["STOPPED"]
      "$or" = [
        { containers = { exitCode = [{ "anything-but" = 0 }] } },
        { stopCode = ["TaskFailedToStart"] },
      ]
    }
  })
}

resource "aws_cloudwatch_event_target" "task_failed_to_sns" {
  rule      = aws_cloudwatch_event_rule.task_failed.name
  target_id = "alerts"
  arn       = aws_sns_topic.alerts.arn

  input_transformer {
    input_paths = {
      task   = "$.detail.taskArn"
      reason = "$.detail.stoppedReason"
      stop   = "$.detail.stopCode"
    }
    input_template = "\"Pipeline ECS task stopped abnormally. task=<task> stopCode=<stop> reason=<reason>\""
  }
}
