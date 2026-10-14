# PoC only: Boundary and Trusted Actions are one regional Terraform plan/state.
# This module is OFF until the former Trusted Actions state is released/imported,
# private TLS and service credentials are reviewed, and the existing workspace
# has been retired. Never enable just by merging this configuration.
module "trusted_actions" {
  count  = var.enable_trusted_actions ? 1 : 0
  source = "../trusted-actions"

  aws_account_id                  = var.aws_account_id
  aws_region                      = var.aws_region
  vpc_id                          = var.vpc_id
  private_subnet_ids              = var.subnet_ids
  boundary_task_security_group_id = aws_security_group.fargate.id
  environment                     = var.stage
  container_image                 = var.trusted_actions_container_image
  s3_bucket_name                  = var.trusted_actions_s3_bucket_name
  backplane_url                   = var.trusted_actions_backplane_url
  backplane_client_id             = var.trusted_actions_backplane_client_id
  backplane_client_secret         = var.trusted_actions_backplane_client_secret
  alb_certificate_arn             = var.trusted_actions_alb_certificate_arn
  allowed_accounts                = var.trusted_actions_allowed_accounts
  default_tags                    = var.default_tags
  ignored_tag_keys                = var.ignored_tag_keys
}

output "trusted_actions_private_alb_dns_name" {
  description = "Private Trusted Actions ALB DNS name; null before the gated state handoff"
  value       = var.enable_trusted_actions ? module.trusted_actions[0].alb_dns_name : null
}
