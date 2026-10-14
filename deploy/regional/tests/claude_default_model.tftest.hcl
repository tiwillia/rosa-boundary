# Verifies the claude_default_model <-> bedrock_model_agreements consistency
# guard in variables.tf: claude_default_model must be a known Bedrock inference
# profile ID (a key in local.claude_inference_profile_foundation_models) that
# resolves to a foundation model present in bedrock_model_agreements. Bare
# foundation model IDs are rejected.
#
# mock_provider avoids any AWS credentials/API calls. The override_data blocks
# supply the few data-source values the module's preconditions and the
# subnet_outbound_routing check assert on, so each run fails (or passes) only on
# the input-variable validation under test.

mock_provider "aws" {
  mock_data "aws_iam_policy_document" {
    defaults = { json = "{\"Version\":\"2012-10-17\",\"Statement\":[]}" }
  }
  # IAM is now read via data sources, so supply valid ARNs at plan time.
  mock_data "aws_iam_role" {
    defaults = { arn = "arn:aws:iam::123456789012:role/rosa-boundary-test" }
  }
  mock_data "aws_iam_openid_connect_provider" {
    defaults = { arn = "arn:aws:iam::123456789012:oidc-provider/auth.example.com/auth/realms/EmployeeIDP" }
  }
}

# Baseline inputs. The approved manifest remains a read-only validation contract;
# account Terraform now owns agreements and offer lookups.
variables {
  aws_account_id       = "123456789012"
  aws_region           = "us-east-1"
  legacy_policy_region = "us-east-1"
  container_image      = "example.com/rosa-boundary:test"
  vpc_id               = "vpc-00000000000000000"
  subnet_ids           = ["subnet-00000000000000000", "subnet-11111111111111111"]
  keycloak_issuer_url  = "https://auth.example.com/auth/realms/EmployeeIDP"
  keycloak_thumbprint  = "0000000000000000000000000000000000000000"
  required_groups      = ["ai-sd-sre"]
  enable_kube_proxy    = false
}

# Real partition so interpolated IAM policy ARNs are valid under mocking.
override_data {
  target = data.aws_partition.current
  values = {
    partition = "aws"
    id        = "aws"
  }
}

override_resource {
  target          = aws_security_group.fargate
  override_during = plan
  values          = { id = "sg-0123456789abcdef0" }
}

# Two subnets in the same VPC, one per AZ (satisfies the Bedrock endpoint
# preconditions: same vpc_id, one distinct AZ per subnet).
override_data {
  target = data.aws_subnet.bedrock_runtime["subnet-00000000000000000"]
  values = {
    vpc_id               = "vpc-00000000000000000"
    availability_zone_id = "use1-az1"
  }
}
override_data {
  target = data.aws_subnet.bedrock_runtime["subnet-11111111111111111"]
  values = {
    vpc_id               = "vpc-00000000000000000"
    availability_zone_id = "use1-az2"
  }
}

# Each subnet's route table has a default route (satisfies subnet_outbound_routing).
override_data {
  target = data.aws_route_table.subnet[0]
  values = {
    routes = [{ cidr_block = "0.0.0.0/0", gateway_id = "igw-0", nat_gateway_id = "" }]
  }
}
override_data {
  target = data.aws_route_table.subnet[1]
  values = {
    routes = [{ cidr_block = "0.0.0.0/0", gateway_id = "igw-0", nat_gateway_id = "" }]
  }
}

# Approved: the Sonnet 5 inference profile maps to foundation model
# anthropic.claude-sonnet-5, which is approved -> validation succeeds.
run "trusted_actions_off_until_state_handoff" {
  command = plan

  assert {
    condition     = length(module.trusted_actions) == 0 && output.trusted_actions_private_alb_dns_name == null
    error_message = "The combined regional root must not deploy Trusted Actions before the state handoff gate is enabled."
  }
}

run "trusted_actions_combined_module_plans_under_mock" {
  command = plan

  variables {
    enable_trusted_actions                  = true
    trusted_actions_container_image         = "example.com/trusted-actions@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    trusted_actions_s3_bucket_name          = "example-trusted-actions-test"
    trusted_actions_backplane_url           = "https://backplane.example.com"
    trusted_actions_backplane_client_id     = "test-client"
    trusted_actions_backplane_client_secret = "mock-only-not-a-real-secret"
    trusted_actions_alb_certificate_arn     = "arn:aws:acm:us-east-1:123456789012:certificate/11111111-1111-1111-1111-111111111111"
    trusted_actions_allowed_accounts        = "123456789012"
  }

  assert {
    condition     = length(module.trusted_actions) == 1
    error_message = "Enabled regional deployment must contain the Trusted Actions child module."
  }

  assert {
    condition     = module.trusted_actions[0].alb_internal && module.trusted_actions[0].alb_ingress_source_security_group_id == aws_security_group.fargate.id
    error_message = "Trusted Actions must use an internal ALB and the Boundary task security group."
  }
}

run "approved_sonnet5_profile_succeeds" {
  command = plan

  variables {
    bedrock_model_agreements = { "anthropic.claude-sonnet-5" = "offer-test" }
    claude_default_model     = "us.anthropic.claude-sonnet-5"
  }

}

# Strictness: a bare foundation model ID is not an inference profile in the
# mapping -> validation fails.
run "bare_foundation_model_rejected" {
  command = plan

  variables {
    bedrock_model_agreements = { "anthropic.claude-sonnet-5" = "offer-test" }
    claude_default_model     = "anthropic.claude-sonnet-5"
  }


  expect_failures = [var.claude_default_model]
}

# Consistency: the Sonnet 5 profile resolves to anthropic.claude-sonnet-5, which
# is absent from bedrock_model_agreements -> validation fails.
run "unapproved_foundation_model_fails" {
  command = plan

  variables {
    bedrock_model_agreements = { "anthropic.claude-opus-5" = "offer-test" }
    claude_default_model     = "us.anthropic.claude-sonnet-5"
  }


  expect_failures = [var.claude_default_model]
}
