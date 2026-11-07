output "state_bucket" {
  description = "Private versioned bucket: input/ (you upload batches), state/ (warehouse checkpoint), locks/, reports/."
  value       = aws_s3_bucket.state.id
}

output "ecs_cluster_arn" {
  value = aws_ecs_cluster.main.arn
}

output "task_definition_arn" {
  value = aws_ecs_task_definition.main.arn
}

output "alert_topic_arn" {
  value = aws_sns_topic.alerts.arn
}

output "log_group" {
  value = aws_cloudwatch_log_group.task.name
}

output "schedule_name" {
  value = aws_scheduler_schedule.pipeline.name
}

output "kms_key_arn" {
  value = aws_kms_key.main.arn
}
