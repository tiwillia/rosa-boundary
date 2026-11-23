# All AWS/ECR/S3 reads are mocked. Distinct source revisions model path-filtered builds.
mock_provider "aws" {
  mock_data "aws_s3_object" {
    defaults = {
      content_type = "application/json"
      body         = file("tests/fixtures/staging-release.json")
    }
  }
  mock_data "aws_caller_identity" {
    defaults = { account_id = "150100906299" }
  }
  mock_data "aws_region" {
    defaults = { region = "us-west-2", name = "us-west-2" }
  }
  mock_data "aws_partition" {
    defaults = { partition = "aws", dns_suffix = "amazonaws.com" }
  }
  mock_data "aws_iam_role" {
    defaults = { arn = "arn:aws:iam::150100906299:role/test" }
  }
  mock_data "aws_iam_openid_connect_provider" {
    defaults = { arn = "arn:aws:iam::150100906299:oidc-provider/auth.example.com/realms/sre" }
  }
  mock_data "aws_kms_alias" {
    defaults = { target_key_arn = "arn:aws:kms:us-west-2:150100906299:key/22222222-2222-2222-2222-222222222222" }
  }
  mock_resource "aws_s3_bucket" {
    defaults = { arn = "arn:aws:s3:::boundary-test-audit" }
  }
}
mock_provider "archive" {}
override_resource {
  target = aws_s3_bucket.content
  values = { arn = "arn:aws:s3:::150100906299-rosa-boundary-stage-us-west-2-content", id = "150100906299-rosa-boundary-stage-us-west-2-content" }
}
override_data {
  target = data.aws_route_table.subnet[0]
  values = { routes = [{ cidr_block = "0.0.0.0/0", gateway_id = "igw-0", nat_gateway_id = "" }] }
}
override_data {
  target = data.aws_route_table.subnet[1]
  values = { routes = [{ cidr_block = "0.0.0.0/0", gateway_id = "igw-0", nat_gateway_id = "" }] }
}
override_data {
  target = data.aws_subnet.bedrock_runtime["subnet-00000000000000000"]
  values = { vpc_id = "vpc-00000000000000000", availability_zone_id = "usw2-az1" }
}
override_data {
  target = data.aws_subnet.bedrock_runtime["subnet-11111111111111111"]
  values = { vpc_id = "vpc-00000000000000000", availability_zone_id = "usw2-az2" }
}

variables {
  staging_release_record_bucket            = "150100906299-rosa-boundary-stage-us-east-1-release-records"
  TFC_CONFIGURATION_VERSION_GIT_COMMIT_SHA = "0000000000000000000000000000000000000001"
  TFC_WORKSPACE_NAME                       = "rosa-boundary-stage-regional-us-west-2"
  stage                                    = "stage"
  aws_account_id                           = "150100906299"
  aws_region                               = "us-west-2"
  legacy_policy_region                     = "us-east-1"
  lambda_package_type                      = "Image"
  enable_kube_proxy                        = true
  vpc_id                                   = "vpc-00000000000000000"
  subnet_ids                               = ["subnet-00000000000000000", "subnet-11111111111111111"]
  keycloak_issuer_url                      = "https://auth.example.com/realms/sre"
  keycloak_thumbprint                      = "0000000000000000000000000000000000000000"
  required_groups                          = ["ai-sd-sre"]
}

run "west_reads_canonical_record_and_selects_regional_images" {
  command = plan
  assert {
    condition = (
      data.aws_s3_object.staging_release[0].region == "us-east-1" &&
      data.aws_s3_object.staging_release[0].bucket == var.staging_release_record_bucket &&
      data.aws_s3_object.staging_release[0].key == "releases/v1/0000000000000000000000000000000000000001.json"
    )
    error_message = "West must read the exact configuration revision from the canonical east bucket."
  }
  assert {
    condition = (
      one([for i in data.aws_ecr_image.staging_lambda : i.registry_id]) == var.aws_account_id &&
      one([for i in data.aws_ecr_image.staging_lambda : i.repository_name]) == "rosa-boundary/create-investigation-lambda" &&
      length(jsondecode(aws_ecs_task_definition.rosa_boundary.container_definitions)) == 2 &&
      alltrue([for c in jsondecode(aws_ecs_task_definition.rosa_boundary.container_definitions) :
        c.image == "quay.io/redhat-user-workloads/rosa-tenant/rosa-boundary@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
      ]) &&
      aws_lambda_function.create_investigation_image[0].image_uri == "150100906299.dkr.ecr.us-west-2.amazonaws.com/rosa-boundary/create-investigation-lambda@sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    )
    error_message = "Both task containers and the regional Lambda must plan with their recorded digests."
  }
}

run "east_uses_east_ecr_and_ignores_explicit_pins" {
  command = plan
  variables {
    TFC_WORKSPACE_NAME = "rosa-boundary-stage-regional"
    aws_region         = "us-east-1"
    container_image    = "example.com/explicit:pin"
    lambda_image_tag   = "0123456789abcdef0123456789abcdef01234567"
  }
  override_data {
    target = data.aws_region.current
    values = { region = "us-east-1", name = "us-east-1" }
  }
  assert {
    condition = (
      aws_lambda_function.create_investigation_image[0].image_uri == "150100906299.dkr.ecr.us-east-1.amazonaws.com/rosa-boundary/create-investigation-lambda@sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb" &&
      local.runtime_image == "quay.io/redhat-user-workloads/rosa-tenant/rosa-boundary@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    )
    error_message = "Existing explicit pins must not affect either image in record mode."
  }
}

run "default_uses_only_explicit_pins" {
  command = plan
  variables {
    staging_release_record_bucket = null
    container_image               = "example.com/explicit:pin"
    lambda_image_tag              = "0123456789abcdef0123456789abcdef01234567"
  }
  assert {
    condition = (
      length(data.aws_s3_object.staging_release) == 0 && length(data.aws_ecr_image.staging_lambda) == 0 &&
      local.runtime_image == var.container_image &&
      aws_lambda_function.create_investigation_image[0].image_uri == "150100906299.dkr.ecr.us-west-2.amazonaws.com/rosa-boundary/create-investigation-lambda:0123456789abcdef0123456789abcdef01234567"
    )
    error_message = "With no opt-in, the existing pins must be used even when HCP supplies a commit SHA."
  }
}

run "rejects_other_bucket" {
  command = plan
  variables {
    staging_release_record_bucket = "untrusted-release-records"
  }
  expect_failures = [var.staging_release_record_bucket]
}

run "rejects_missing_revision" {
  command = plan
  variables {
    TFC_CONFIGURATION_VERSION_GIT_COMMIT_SHA = null
  }
  expect_failures = [data.aws_s3_object.staging_release[0]]
}

run "rejects_other_revision" {
  command = plan
  variables {
    TFC_CONFIGURATION_VERSION_GIT_COMMIT_SHA = "0000000000000000000000000000000000000004"
  }
  expect_failures = [data.aws_s3_object.staging_release[0]]
}

run "rejects_workspace_region_mismatch" {
  command = plan
  variables {
    TFC_WORKSPACE_NAME = "rosa-boundary-stage-regional"
  }
  expect_failures = [data.aws_s3_object.staging_release[0]]
}

run "rejects_other_account" {
  command = plan
  variables {
    aws_account_id = "514903006123"
  }
  expect_failures = [data.aws_s3_object.staging_release[0]]
}

run "rejects_unusable_record_even_with_explicit_pins" {
  command = plan
  variables {
    container_image  = "example.com/explicit:pin"
    lambda_image_tag = "0123456789abcdef0123456789abcdef01234567"
  }
  override_data {
    target = data.aws_s3_object.staging_release[0]
    values = { content_type = "application/json", body = "not-json" }
  }
  expect_failures = [data.aws_s3_object.staging_release[0]]
}

run "rejects_lookalike_runtime_repository" {
  command = plan
  override_data {
    target = data.aws_s3_object.staging_release[0]
    values = { content_type = "application/json", body = replace(file("tests/fixtures/staging-release.json"), "rosa-tenant/", "rosa-tenant-lookalike/") }
  }
  expect_failures = [data.aws_s3_object.staging_release[0]]
}

run "rejects_qualified_lambda_repository" {
  command = plan
  override_data {
    target = data.aws_s3_object.staging_release[0]
    values = { content_type = "application/json", body = replace(file("tests/fixtures/staging-release.json"), "rosa-boundary/create-investigation-lambda", "other.example/rosa-boundary/create-investigation-lambda") }
  }
  expect_failures = [data.aws_s3_object.staging_release[0]]
}

run "rejects_mutable_image_identity" {
  command = plan
  override_data {
    target = data.aws_s3_object.staging_release[0]
    values = { content_type = "application/json", body = replace(file("tests/fixtures/staging-release.json"), "sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb", "latest") }
  }
  expect_failures = [data.aws_s3_object.staging_release[0]]
}

run "rejects_missing_local_region" {
  command = plan
  override_data {
    target = data.aws_s3_object.staging_release[0]
    values = { content_type = "application/json", body = replace(file("tests/fixtures/staging-release.json"), "us-west-2", "us-west-1") }
  }
  expect_failures = [data.aws_s3_object.staging_release[0]]
}

run "rejects_invalid_evidence_id" {
  command = plan
  override_data {
    target = data.aws_s3_object.staging_release[0]
    values = { content_type = "application/json", body = replace(file("tests/fixtures/staging-release.json"), "check/88", "check pending") }
  }
  expect_failures = [data.aws_s3_object.staging_release[0]]
}

run "east_only_evidence_cannot_authorize_east" {
  command = plan
  variables {
    TFC_WORKSPACE_NAME = "rosa-boundary-stage-regional"
    aws_region         = "us-east-1"
  }
  override_data {
    target = data.aws_s3_object.staging_release[0]
    values = { content_type = "application/json", body = replace(file("tests/fixtures/staging-release.json"), ", \"us-west-2\"", "") }
  }
  expect_failures = [data.aws_s3_object.staging_release[0]]
}
