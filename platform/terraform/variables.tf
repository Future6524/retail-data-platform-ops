variable "aws_region" {
  description = "AWS region to deploy into (you choose; no default account or region data is embedded)."
  type        = string
}

variable "name_prefix" {
  description = "Prefix for resource names. Lower-case letters, digits and hyphens."
  type        = string
  default     = "retail-pipeline"

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{2,30}$", var.name_prefix))
    error_message = "name_prefix must be 3-31 chars: lower-case letters, digits, hyphens, starting with a letter."
  }
}

variable "container_image" {
  description = "Fully qualified image you built and pushed yourself (this repo's Dockerfile). Prefer an immutable digest: repo@sha256:..."
  type        = string

  validation {
    condition     = !endswith(var.container_image, ":latest") && length(var.container_image) > 0
    error_message = "container_image must be set and must not use the mutable :latest tag."
  }
}

variable "ecr_repository_arn" {
  description = "ARN of the ECR repository holding container_image. When set, the execution role may pull ONLY from it. Leave empty for non-ECR registries."
  type        = string
  default     = ""
}

# --- networking: supplied by you; this module creates no VPC, subnets, gateways or security groups ---
variable "subnet_ids" {
  description = "Subnets for the Fargate task (existing, user-controlled). Need egress to S3, KMS, CloudWatch Logs, ECR (and Secrets Manager if used), e.g. via NAT or VPC endpoints."
  type        = list(string)

  validation {
    condition     = length(var.subnet_ids) > 0 && alltrue([for s in var.subnet_ids : can(regex("^subnet-[0-9a-f]{8,17}$", s))])
    error_message = "Provide at least one real subnet id (subnet-xxxxxxxx...)."
  }
}

variable "security_group_ids" {
  description = "Security groups for the task (existing, user-controlled). Recommended: no inbound rules, egress 443 only."
  type        = list(string)

  validation {
    condition     = length(var.security_group_ids) > 0 && alltrue([for s in var.security_group_ids : can(regex("^sg-[0-9a-f]{8,17}$", s))])
    error_message = "Provide at least one real security group id (sg-xxxxxxxx...)."
  }
}

variable "assign_public_ip" {
  description = "Give the task a public IP. Keep false when subnets have NAT/VPC endpoints."
  type        = bool
  default     = false
}

# --- scheduling ---
variable "schedule_expression" {
  description = "EventBridge Scheduler expression, e.g. rate(1 day) or cron(0 3 * * ? *) (UTC)."
  type        = string
  default     = "cron(0 3 * * ? *)"
}

variable "schedule_enabled" {
  description = "Create the schedule ENABLED. Default false so applying never runs anything until you opt in."
  type        = bool
  default     = false
}

variable "task_cpu" {
  type    = number
  default = 512
}

variable "task_memory" {
  type    = number
  default = 1024
}

variable "ephemeral_storage_gib" {
  description = "Scratch disk for the DuckDB file + downloaded inputs (21-200)."
  type        = number
  default     = 30

  validation {
    condition     = var.ephemeral_storage_gib >= 21 && var.ephemeral_storage_gib <= 200
    error_message = "Fargate ephemeral storage must be 21-200 GiB."
  }
}

variable "task_timeout_seconds" {
  description = "Hard stop for a run (the lease lock TTL must stay above this)."
  type        = number
  default     = 3600
}

# --- state / retention ---
variable "noncurrent_version_retention_days" {
  description = "How long old versions of the warehouse checkpoint are kept for rollback."
  type        = number
  default     = 30
}

variable "log_retention_days" {
  type    = number
  default = 365
}

# --- alerting ---
variable "alert_email" {
  description = "Optional e-mail for the SNS alert topic (needs manual confirmation by the recipient). Empty = no subscription."
  type        = string
  default     = ""
}

variable "max_hours_without_success" {
  description = "Alarm if no successful run is logged for this many hours (set above your schedule period)."
  type        = number
  default     = 36

  validation {
    condition     = var.max_hours_without_success >= 2 && var.max_hours_without_success <= 160
    error_message = "Use 2-160 hours."
  }
}

# --- secrets (optional, by reference only) ---
variable "secret_arns" {
  description = <<-EOT
    Map of ENVIRONMENT_VARIABLE_NAME => Secrets Manager secret ARN (optionally with a ':json-key::' suffix) that the
    task should receive. You create these secrets and their values OUTSIDE Terraform (e.g. `aws secretsmanager
    put-secret-value`), so no secret value ever reaches this code or the state. The execution role is granted
    GetSecretValue on exactly these ARNs. The default DuckDB pipeline needs none; empty by default.
  EOT
  type        = map(string)
  default     = {}

  validation {
    condition     = alltrue([for k, v in var.secret_arns : can(regex("^[A-Z][A-Z0-9_]*$", k)) && can(regex("^arn:aws[a-z-]*:secretsmanager:[a-z0-9-]+:[0-9]{12}:secret:[A-Za-z0-9/_+=.@-]+(:.*)?$", v))])
    error_message = "Keys must be UPPER_SNAKE env names; values must be Secrets Manager ARNs."
  }
}

variable "secrets_kms_key_arn" {
  description = "KMS key ARN that encrypts the secrets in secret_arns (if customer-managed); the execution role gets kms:Decrypt on it. Empty = AWS-managed key."
  type        = string
  default     = ""
}

variable "cpu_architecture" {
  description = "Must match the architecture your image was built for."
  type        = string
  default     = "X86_64"

  validation {
    condition     = contains(["X86_64", "ARM64"], var.cpu_architecture)
    error_message = "cpu_architecture must be X86_64 or ARM64."
  }
}
