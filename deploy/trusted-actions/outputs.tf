output "alb_dns_name" {
  description = "Private ALB DNS name (reachable only from allowed VPC tasks)"
  value       = aws_lb.main.dns_name
}

output "alb_internal" {
  description = "Whether the API ALB is configured as internal"
  value       = aws_lb.main.internal
}

output "alb_ingress_source_security_group_id" {
  description = "Boundary task SG supplying private API ingress"
  value       = var.boundary_task_security_group_id
}

output "ecs_cluster_name" {
  description = "ECS cluster name"
  value       = aws_ecs_cluster.main.name
}

output "cloudwatch_log_group" {
  description = "CloudWatch log group name for container logs"
  value       = aws_cloudwatch_log_group.app.name
}
