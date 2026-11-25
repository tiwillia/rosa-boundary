# Mocked, targeted plans test storage guards without AWS or HCP state access.
mock_provider "aws" {
  mock_data "aws_vpc" {
    defaults = {
      cidr_block           = "10.80.0.0/20"
      enable_dns_support   = true
      enable_dns_hostnames = true
    }
  }
  mock_data "aws_caller_identity" {
    defaults = { account_id = "123456789012" }
  }
}

variables {
  container_image = "example.com/trusted-actions:test"
  environment     = "dev"
}

override_data {
  target = data.terraform_remote_state.global
  values = {
    outputs = {
      secretsmanager_secret_arn        = "arn:aws:secretsmanager:us-east-1:123456789012:secret:test"
      iam_role_task_arn                = "arn:aws:iam::123456789012:role/test-task"
      iam_role_task_execution_arn      = "arn:aws:iam::123456789012:role/test-execution"
      iam_instance_profile_name        = "test-instance"
      iam_role_cwl_to_firehose_arn     = "arn:aws:iam::123456789012:role/test-cwl"
      iam_role_firehose_audit_logs_arn = "arn:aws:iam::123456789012:role/test-firehose"
      internal_fqdn                    = ""
      public_zone_id                   = ""
    }
  }
}

run "private_http_without_certificate_keeps_authentication_enabled" {
  command = plan
  variables {
    ephemeral_environment = true
    vpc_id                = "vpc-test"
    public_subnet_ids     = ["subnet-public-a", "subnet-public-b"]
    private_subnet_ids    = ["subnet-private-a", "subnet-private-b"]
  }
  plan_options {
    target = [aws_lb.main, aws_lb_listener.http, aws_lb_listener.https, aws_acm_certificate.app, aws_route53_zone.private, aws_ecs_task_definition.app]
  }
  assert {
    condition = (
      aws_lb.main.internal &&
      one(aws_lb_listener.http.default_action).type == "forward" &&
      length(aws_lb_listener.https) == 0 &&
      length(aws_acm_certificate.app) == 0 &&
      length(aws_route53_zone.private) == 0 &&
      one([for e in jsondecode(aws_ecs_task_definition.app.container_definitions)[1].environment : e.value if e.name == "ROSA_TA_ENABLE_AUTH"]) == "true"
    )
    error_message = "HTTP dev must use an internal ALB without certificate/custom DNS while preserving TA authentication."
  }
  assert {
    condition = alltrue([
      for rule in aws_security_group.alb.ingress :
      toset(rule.cidr_blocks) == toset(["10.80.0.0/20"])
    ])
    error_message = "HTTP and HTTPS ALB ingress must remain confined to the VPC CIDR."
  }
}

run "optional_tls_uses_certificate_and_redirects_http" {
  command = plan
  variables {
    ephemeral_environment = true
    vpc_id                = "vpc-test"
    public_subnet_ids     = ["subnet-public-a", "subnet-public-b"]
    private_subnet_ids    = ["subnet-private-a", "subnet-private-b"]
    internal_fqdn         = "ta.example.com"
    alb_certificate_arn   = "arn:aws:acm:us-east-1:123456789012:certificate/test"
  }
  plan_options {
    target = [aws_lb.main, aws_lb_listener.http, aws_lb_listener.https, aws_acm_certificate.app, aws_route53_zone.private]
  }
  assert {
    condition = (
      aws_lb.main.internal &&
      one(aws_lb_listener.http.default_action).type == "redirect" &&
      one(one(aws_lb_listener.http.default_action).redirect).protocol == "HTTPS" &&
      aws_lb_listener.https[0].certificate_arn == var.alb_certificate_arn &&
      length(aws_acm_certificate.app) == 0 &&
      length(aws_route53_zone.private) == 1
    )
    error_message = "Optional TLS must keep the ALB private, use the supplied certificate and redirect HTTP."
  }
}

run "persistent_defaults" {
  command = plan
  plan_options {
    target = [aws_s3_bucket_object_lock_configuration.app, aws_s3_bucket.app, aws_cloudwatch_log_group.app, aws_cloudwatch_log_group.firehose_audit_logs]
  }
  assert {
    condition = (
      aws_s3_bucket.app.object_lock_enabled &&
      !aws_s3_bucket.app.force_destroy &&
      aws_cloudwatch_log_group.app.retention_in_days == 30 &&
      aws_cloudwatch_log_group.firehose_audit_logs.retention_in_days == 90
    )
    error_message = "Persistent bucket and log-retention defaults must not change."
  }
}

run "persistent_worm_retained" {
  command = plan
  variables {
    enable_worm = true
  }
  plan_options {
    target = [aws_s3_bucket_object_lock_configuration.app]
  }
  assert {
    condition     = length(aws_s3_bucket_object_lock_configuration.app) == 1 && aws_s3_bucket_object_lock_configuration.app[0].rule[0].default_retention[0].mode == "COMPLIANCE"
    error_message = "Explicit persistent WORM protection must remain intact."
  }
}

run "task_roles_use_original_global_state_contract" {
  command = plan
  plan_options {
    target = [aws_ecs_task_definition.app]
  }
  assert {
    condition = (
      aws_ecs_task_definition.app.task_role_arn == data.terraform_remote_state.global.outputs.iam_role_task_arn &&
      aws_ecs_task_definition.app.execution_role_arn == data.terraform_remote_state.global.outputs.iam_role_task_execution_arn
    )
    error_message = "Task roles must consume the original separate global remote-state outputs."
  }
}

run "global_state_contract_has_no_dev_only_exception" {
  command = plan
  variables {
    environment = "prod"
  }
  plan_options {
    target = [aws_ecs_task_definition.app]
  }
  assert {
    condition = (
      aws_ecs_task_definition.app.task_role_arn == data.terraform_remote_state.global.outputs.iam_role_task_arn &&
      aws_ecs_task_definition.app.execution_role_arn == data.terraform_remote_state.global.outputs.iam_role_task_execution_arn &&
      data.terraform_remote_state.global.backend == "remote"
    )
    error_message = "Production must retain the same remote-state output contract without a dev-only input exception."
  }
}

run "explicit_dev_is_ephemeral" {
  command = plan
  variables {
    ephemeral_environment = true
    enable_worm           = false
  }
  plan_options {
    target = [aws_s3_bucket.app, aws_s3_bucket_object_lock_configuration.app, aws_s3_bucket_public_access_block.app, aws_s3_bucket_server_side_encryption_configuration.app, aws_cloudwatch_log_group.app, aws_cloudwatch_log_group.firehose_audit_logs]
  }
  assert {
    condition = (
      aws_s3_bucket.app.force_destroy &&
      aws_s3_bucket.app.object_lock_enabled &&
      length(aws_s3_bucket_object_lock_configuration.app) == 0 &&
      aws_s3_bucket_public_access_block.app.block_public_policy &&
      one(aws_s3_bucket_server_side_encryption_configuration.app.rule).apply_server_side_encryption_by_default[0].sse_algorithm == "AES256" &&
      aws_cloudwatch_log_group.app.retention_in_days == 30 &&
      aws_cloudwatch_log_group.firehose_audit_logs.retention_in_days == 90
    )
    error_message = "Ephemeral dev without retention must preserve Object Lock capability, encryption, public-access blocking, and the original 30/90-day log retention."
  }
}

run "ephemeral_dev_preserves_explicit_worm_retention" {
  command = plan
  variables {
    ephemeral_environment = true
    enable_worm           = true
  }
  plan_options {
    target = [aws_s3_bucket.app, aws_s3_bucket_object_lock_configuration.app]
  }
  assert {
    condition = (
      aws_s3_bucket.app.force_destroy &&
      aws_s3_bucket.app.object_lock_enabled &&
      length(aws_s3_bucket_object_lock_configuration.app) == 1 &&
      aws_s3_bucket_object_lock_configuration.app[0].rule[0].default_retention[0].mode == "COMPLIANCE"
    )
    error_message = "Dev force_destroy must never silently disable explicitly requested WORM retention."
  }
}

run "stage_rejected" {
  command = plan
  variables {
    environment           = "stage"
    ephemeral_environment = true
  }
  plan_options {
    target = [aws_s3_bucket.app]
  }
  expect_failures = [aws_s3_bucket.app]
}

run "prod_rejected" {
  command = plan
  variables {
    environment           = "prod"
    ephemeral_environment = true
  }
  plan_options {
    target = [aws_s3_bucket.app]
  }
  expect_failures = [aws_s3_bucket.app]
}
