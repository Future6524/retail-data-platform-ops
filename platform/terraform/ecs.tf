resource "aws_ecs_cluster" "main" {
  name = var.name_prefix

  setting {
    name  = "containerInsights"
    value = "enabled"
  }
}

resource "aws_ecs_task_definition" "main" {
  family                   = var.name_prefix
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.task_cpu
  memory                   = var.task_memory
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.task.arn

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = var.cpu_architecture
  }

  ephemeral_storage {
    size_in_gib = var.ephemeral_storage_gib
  }

  # Writable scratch for the working copy of the DuckDB file; the container root FS itself is read-only.
  volume {
    name = "scratch"
  }

  container_definitions = jsonencode([
    {
      name                   = "pipeline"
      image                  = var.container_image
      essential              = true
      user                   = "10001:10001"
      readonlyRootFilesystem = true
      command                = ["task"] # image ENTRYPOINT = python -m retail_ops
      stopTimeout            = 120

      environment = [
        { name = "RETAIL_STATE_BUCKET", value = aws_s3_bucket.state.id },
        { name = "RETAIL_STATE_KEY", value = local.state_key },
        { name = "RETAIL_LOCK_KEY", value = local.lock_key },
        { name = "RETAIL_INPUT_PREFIX", value = local.input_prefix },
        { name = "RETAIL_REPORT_PREFIX", value = local.report_prefix },
        { name = "RETAIL_WORK_DIR", value = "/work" },
        { name = "RETAIL_TIMEOUT_SECONDS", value = tostring(var.task_timeout_seconds) },
        { name = "RETAIL_LOCK_TTL_SECONDS", value = tostring(var.task_timeout_seconds + 600) },
      ]

      # Secrets are injected by ECS from Secrets Manager by ARN reference. No value exists in this code or state.
      secrets = [for name, arn in var.secret_arns : { name = name, valueFrom = arn }]

      mountPoints = [{ sourceVolume = "scratch", containerPath = "/work", readOnly = false }]

      linuxParameters = {
        initProcessEnabled = true
        capabilities       = { drop = ["ALL"] }
      }

      logConfiguration = {
        logDriver = "awslogs"
        options = {
          "awslogs-group"         = aws_cloudwatch_log_group.task.name
          "awslogs-region"        = local.region
          "awslogs-stream-prefix" = "task"
        }
      }
    }
  ])
}
