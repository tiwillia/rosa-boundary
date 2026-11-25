# Configuration-only tests: mocked AWS provider, targeted plans, no live state.
mock_provider "aws" {
  mock_data "aws_partition" {
    defaults = { partition = "aws", dns_suffix = "amazonaws.com" }
  }
  mock_data "aws_caller_identity" {
    defaults = { account_id = "123456789012" }
  }
  mock_data "aws_region" {
    defaults = { region = "us-east-1", name = "us-east-1" }
  }
  mock_data "aws_iam_role" {
    defaults = { arn = "arn:aws:iam::123456789012:role/boundary-shared" }
  }
}
mock_provider "archive" {}

variables {
  aws_account_id       = "123456789012"
  aws_region           = "us-east-1"
  legacy_policy_region = "us-east-1"
  container_image      = "example.com/rosa-boundary:test"
  vpc_id               = "vpc-00000000000000000"
  subnet_ids           = ["subnet-00000000000000000", "subnet-11111111111111111"]
  keycloak_issuer_url  = "https://auth.example.com/realms/sre"
  keycloak_thumbprint  = "0000000000000000000000000000000000000000"
  required_groups      = ["ai-sd-sre"]
}

run "persistent_defaults" {
  command = plan
  plan_options {
    target = [aws_s3_bucket_object_lock_configuration.audit, aws_s3_bucket.bedrock_large_payloads, aws_s3_bucket.content, aws_kms_key.bedrock_invocations]
  }
  assert {
    condition = (
      aws_kms_key.bedrock_invocations.deletion_window_in_days == 30 &&
      !aws_s3_bucket.audit.force_destroy &&
      aws_s3_bucket.bedrock_large_payloads.force_destroy != true &&
      !aws_s3_bucket.content.force_destroy &&
      length(aws_s3_bucket_object_lock_configuration.audit) == 1 &&
      aws_s3_bucket_object_lock_configuration.audit[0].rule[0].default_retention[0].mode == "COMPLIANCE"
    )
    error_message = "Default deployments must retain WORM audit retention and buckets protected from forced deletion."
  }
}

run "explicit_dev_is_ephemeral" {
  command = plan
  variables {
    ephemeral_environment        = true
    content_bucket_force_destroy = true
    stage                        = "dev"
  }
  plan_options {
    target = [aws_s3_bucket.audit, aws_s3_bucket_object_lock_configuration.audit, aws_s3_bucket.bedrock_large_payloads, aws_s3_bucket.content, aws_s3_bucket_public_access_block.audit, aws_s3_bucket_server_side_encryption_configuration.audit, aws_kms_key.bedrock_invocations]
  }
  assert {
    condition = (
      aws_kms_key.bedrock_invocations.deletion_window_in_days == 30 &&
      aws_s3_bucket.audit.force_destroy &&
      aws_s3_bucket.bedrock_large_payloads.force_destroy != true &&
      aws_s3_bucket.content.force_destroy &&
      length(aws_s3_bucket_object_lock_configuration.audit) == 0 &&
      aws_s3_bucket_public_access_block.audit.block_public_policy &&
      one(aws_s3_bucket_server_side_encryption_configuration.audit.rule).apply_server_side_encryption_by_default[0].sse_algorithm == "AES256"
    )
    error_message = "Ephemeral dev must preserve Bedrock storage and its 30-day KMS deletion window, public-access blocking, and audit encryption."
  }
}

run "stage_rejects_ephemeral_buckets" {
  command = plan
  variables {
    ephemeral_environment = true
    stage                 = "stage"
  }
  plan_options {
    target = [aws_s3_bucket.audit, aws_s3_bucket.bedrock_large_payloads, aws_s3_bucket.content, aws_kms_key.bedrock_invocations]
  }
  expect_failures = [aws_s3_bucket.audit]
}

run "prod_rejects_ephemeral_buckets" {
  command = plan
  variables {
    ephemeral_environment = true
    stage                 = "prod"
  }
  plan_options {
    target = [aws_s3_bucket.audit, aws_s3_bucket.bedrock_large_payloads, aws_s3_bucket.content, aws_kms_key.bedrock_invocations]
  }
  expect_failures = [aws_s3_bucket.audit]
}

run "ephemeral_dev_keeps_standard_bedrock_logging" {
  command = plan
  variables {
    ephemeral_environment = true
    stage                 = "dev"
  }
  plan_options {
    target = [aws_bedrock_model_invocation_logging_configuration.boundary]
  }
  assert {
    condition = (
      aws_bedrock_model_invocation_logging_configuration.boundary.logging_config[0].text_data_delivery_enabled &&
      !aws_bedrock_model_invocation_logging_configuration.boundary.logging_config[0].image_data_delivery_enabled &&
      !aws_bedrock_model_invocation_logging_configuration.boundary.logging_config[0].embedding_data_delivery_enabled &&
      !aws_bedrock_model_invocation_logging_configuration.boundary.logging_config[0].video_data_delivery_enabled &&
      aws_bedrock_model_invocation_logging_configuration.boundary.logging_config[0].cloudwatch_config[0].log_group_name == aws_cloudwatch_log_group.bedrock_invocations.name &&
      aws_cloudwatch_log_group.bedrock_invocations.retention_in_days == 90 &&
      aws_kms_key.bedrock_invocations.deletion_window_in_days == 30 &&
      aws_kms_key.bedrock_invocations.enable_key_rotation &&
      aws_s3_bucket.bedrock_large_payloads.force_destroy != true
    )
    error_message = "Ephemeral dev must retain standard Bedrock text logging, 90-day log retention, 30-day KMS deletion, key rotation, and persistent large-payload storage."
  }
}

run "prod_keeps_standard_bedrock_logging" {
  command = plan
  variables {
    stage = "prod"
  }
  plan_options {
    target = [aws_bedrock_model_invocation_logging_configuration.boundary]
  }
  assert {
    condition = (
      aws_bedrock_model_invocation_logging_configuration.boundary.logging_config[0].text_data_delivery_enabled &&
      aws_bedrock_model_invocation_logging_configuration.boundary.logging_config[0].cloudwatch_config[0].log_group_name == aws_cloudwatch_log_group.bedrock_invocations.name &&
      aws_cloudwatch_log_group.bedrock_invocations.retention_in_days == 90 &&
      aws_kms_key.bedrock_invocations.deletion_window_in_days == 30 &&
      aws_kms_key.bedrock_invocations.enable_key_rotation &&
      aws_s3_bucket.bedrock_large_payloads.force_destroy != true
    )
    error_message = "Production must always retain standard Bedrock logging and storage protection."
  }
}
