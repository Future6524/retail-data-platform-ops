resource "aws_sqs_queue" "scheduler_dlq" {
  name                      = "${var.name_prefix}-scheduler-dlq"
  message_retention_seconds = 1209600
  sqs_managed_sse_enabled   = true
}

resource "aws_scheduler_schedule" "pipeline" {
  # checkov:skip=CKV_AWS_297:The schedule payload holds no secrets (subnet/SG ids and ARNs only); the default AWS-owned key is accepted.
  name                         = var.name_prefix
  description                  = "Runs the SYNTHETIC-data retail pipeline Fargate task."
  schedule_expression          = var.schedule_expression
  schedule_expression_timezone = "UTC"
  state                        = var.schedule_enabled ? "ENABLED" : "DISABLED"

  flexible_time_window {
    mode = "OFF"
  }

  target {
    arn      = aws_ecs_cluster.main.arn
    role_arn = aws_iam_role.scheduler.arn

    ecs_parameters {
      task_definition_arn = aws_ecs_task_definition.main.arn
      launch_type         = "FARGATE"
      platform_version    = "LATEST"
      task_count          = 1

      network_configuration {
        subnets          = var.subnet_ids
        security_groups  = var.security_group_ids
        assign_public_ip = var.assign_public_ip
      }
    }

    retry_policy {
      maximum_event_age_in_seconds = 3600
      maximum_retry_attempts       = 2
    }

    dead_letter_config {
      arn = aws_sqs_queue.scheduler_dlq.arn
    }
  }
}
