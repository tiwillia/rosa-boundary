mock_provider "aws" {
  mock_data "aws_partition" {
    defaults = { partition = "aws", dns_suffix = "amazonaws.com" }
  }
}

variables {
  stage                             = "stage"
  aws_account_id                    = "150100906299"
  aws_region                        = "us-east-1"
  legacy_policy_region              = "us-east-1"
  legacy_role_tag_region            = "us-east-1"
  bedrock_budget_tag_region         = "us-east-1"
  bedrock_monthly_budget_usd        = 1000
  bedrock_budget_notification_email = "alerts@example.com"
  keycloak_issuer_url               = "https://auth.example.com/realms/sre"
  keycloak_thumbprint               = "0000000000000000000000000000000000000000"
}

run "no_release_resources_by_default" {
  command = plan
  assert {
    condition = (
      output.release_record_bucket_name == null &&
      length(aws_s3_bucket.release_records) == 0 &&
      length(aws_s3_bucket_policy.release_records) == 0 &&
      length(aws_s3_bucket_versioning.release_records) == 0 &&
      length(aws_s3_bucket_server_side_encryption_configuration.release_records) == 0 &&
      length(aws_s3_bucket_public_access_block.release_records) == 0 &&
      length(aws_s3_bucket_ownership_controls.release_records) == 0
    )
    error_message = "Account workspaces must have no release-record resources unless explicitly enabled."
  }
}

run "staging_bucket_and_write_once_policy" {
  command = plan
  variables {
    enable_release_record_bucket      = true
    release_record_publisher_role_arn = "arn:aws:iam::150100906299:role/test/publisher"
  }
  assert {
    condition = (
      output.release_record_bucket_name == "150100906299-rosa-boundary-stage-us-east-1-release-records" &&
      aws_s3_bucket.release_records[0].force_destroy == false &&
      aws_s3_bucket_versioning.release_records[0].versioning_configuration[0].status == "Enabled" &&
      one(aws_s3_bucket_server_side_encryption_configuration.release_records[0].rule).apply_server_side_encryption_by_default[0].sse_algorithm == "AES256" &&
      aws_s3_bucket_ownership_controls.release_records[0].rule[0].object_ownership == "BucketOwnerEnforced" &&
      aws_s3_bucket_public_access_block.release_records[0].block_public_acls &&
      aws_s3_bucket_public_access_block.release_records[0].ignore_public_acls &&
      aws_s3_bucket_public_access_block.release_records[0].block_public_policy &&
      aws_s3_bucket_public_access_block.release_records[0].restrict_public_buckets
    )
    error_message = "Release records require private versioned durable storage in the agreed bucket."
  }
  assert {
    condition = alltrue([
      for s in jsondecode(aws_s3_bucket_policy.release_records[0].policy).Statement :
      s.Principal.AWS == var.release_record_publisher_role_arn &&
      toset(s.Action) == toset(["s3:GetObject", "s3:PutObject"]) &&
      s.Resource == "arn:aws:s3:::${output.release_record_bucket_name}/releases/v1/????????????????????????????????????????.json"
      if s.Effect == "Allow"
    ]) && length([for s in jsondecode(aws_s3_bucket_policy.release_records[0].policy).Statement : s if s.Effect == "Allow"]) == 1
    error_message = "The sole resource grant must let only the named publisher create or verify a release record."
  }
  assert {
    condition = alltrue([for restriction in [
      { Action = "s3:PutObject", Condition = { ArnNotEquals = { "aws:PrincipalArn" = var.release_record_publisher_role_arn } } },
      { Action = "s3:PutObject", Condition = { StringNotEquals = { "s3:if-none-match" = "*" } } },
      { Action = ["s3:DeleteObject", "s3:DeleteObjectVersion", "s3:ReplicateObject", "s3:ReplicateDelete"] },
      ] : anytrue([for s in jsondecode(aws_s3_bucket_policy.release_records[0].policy).Statement :
        s.Effect == "Deny" && s.Principal == "*" && s.Resource == "arn:aws:s3:::${output.release_record_bucket_name}/*" &&
        jsonencode({ for k, v in s : k => v if contains(["Action", "Condition"], k) }) == jsonencode(restriction)
    ])])
    error_message = "Same-account IAM grants must not bypass the publisher, conditional write, version deletion or replication denies."
  }
}

run "require_publisher" {
  command = plan
  variables {
    enable_release_record_bucket = true
  }
  expect_failures = [var.release_record_publisher_role_arn]
}

run "reject_wildcard_publisher" {
  command = plan
  variables {
    enable_release_record_bucket      = true
    release_record_publisher_role_arn = "arn:aws:iam::150100906299:role/test/*"
  }
  expect_failures = [var.release_record_publisher_role_arn]
}

run "reject_prod_stage" {
  command = plan
  variables {
    stage                             = "prod"
    enable_release_record_bucket      = true
    release_record_publisher_role_arn = "arn:aws:iam::150100906299:role/test/publisher"
  }
  expect_failures = [var.enable_release_record_bucket]
}

run "reject_other_account" {
  command = plan
  variables {
    aws_account_id                    = "123456789012"
    enable_release_record_bucket      = true
    release_record_publisher_role_arn = "arn:aws:iam::150100906299:role/test/publisher"
  }
  expect_failures = [var.enable_release_record_bucket]
}

run "reject_replica_region" {
  command = plan
  variables {
    aws_region                        = "us-west-2"
    enable_release_record_bucket      = true
    release_record_publisher_role_arn = "arn:aws:iam::150100906299:role/test/publisher"
  }
  expect_failures = [var.enable_release_record_bucket]
}
