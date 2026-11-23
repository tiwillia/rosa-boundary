# This read has only plan-time inputs. Do not add a dependency on a managed
# resource: the digest pair belongs in the saved plan, not an apply-time lookup.
locals {
  use_staging_release_record = var.staging_release_record_bucket != null
  staging_regional_workspaces = {
    us-east-1 = "rosa-boundary-stage-regional"
    us-west-2 = "rosa-boundary-stage-regional-us-west-2"
  }
  staging_runtime_repository = "quay.io/redhat-user-workloads/rosa-tenant/rosa-boundary"
  staging_lambda_repository  = "rosa-boundary/create-investigation-lambda"
  git_sha_pattern            = "^[0-9a-f]{40}$"
  digest_pattern             = "^sha256:[0-9a-f]{64}$"
  evidence_id_pattern        = "^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$"

  staging_release = local.use_staging_release_record ? jsondecode(data.aws_s3_object.staging_release[0].body) : null
  runtime_image   = local.use_staging_release_record ? "${local.staging_runtime_repository}@${local.staging_release.runtime.digest}" : var.container_image
}

# A missing or expired replica must fail this Region's plan. Using the digest
# as the instance key also makes deferring the immutable record to apply invalid.
data "aws_ecr_image" "staging_lambda" {
  for_each        = local.use_staging_release_record ? toset([local.staging_release.lambda.digest]) : toset([])
  registry_id     = var.aws_account_id
  repository_name = local.staging_lambda_repository
  image_digest    = each.key
}

data "aws_s3_object" "staging_release" {
  count  = local.use_staging_release_record ? 1 : 0
  region = "us-east-1" # Canonical record, including when planning the replica.
  bucket = var.staging_release_record_bucket
  key    = "releases/v1/${var.TFC_CONFIGURATION_VERSION_GIT_COMMIT_SHA}.json"

  lifecycle {
    precondition {
      condition = (
        var.project == "rosa-boundary" && var.stage == "stage" && var.aws_account_id == "150100906299" &&
        try(local.staging_regional_workspaces[var.aws_region] == var.TFC_WORKSPACE_NAME, false) &&
        local.lambda_use_image
      )
      error_message = "Release records require a Boundary staging regional HCP workspace, its matching Region and account, and Image packaging."
    }
    precondition {
      condition     = can(regex(local.git_sha_pattern, var.TFC_CONFIGURATION_VERSION_GIT_COMMIT_SHA))
      error_message = "Release records require the run's exact 40-character lowercase Git commit SHA."
    }
    postcondition {
      condition = try(
        self.content_type == "application/json" &&
        jsondecode(self.body).schema_version == 1 &&
        jsondecode(self.body).infrastructure_revision == var.TFC_CONFIGURATION_VERSION_GIT_COMMIT_SHA &&
        jsondecode(self.body).staging_account_id == var.aws_account_id &&
        jsondecode(self.body).regions == ["us-east-1", "us-west-2"],
        false
      )
      error_message = "Expected a v1 application/json staging record for this exact commit, account and Region."
    }
    postcondition {
      condition = try(
        jsondecode(self.body).runtime.repository == local.staging_runtime_repository &&
        jsondecode(self.body).lambda.repository == local.staging_lambda_repository &&
        alltrue([for artifact in [jsondecode(self.body).runtime, jsondecode(self.body).lambda] :
          artifact.source_revision == regex(local.git_sha_pattern, artifact.source_revision) &&
          can(regex(local.digest_pattern, artifact.digest))
        ]),
        false
      )
      error_message = "Both artifacts must designate the staging repositories, a full lowercase source SHA and an immutable sha256 digest."
    }
    postcondition {
      condition = try(alltrue([
        for key in ["release_id", "compatibility_id", "runtime_build_id", "lambda_build_id"] :
        jsondecode(self.body).release_evidence[key] == regex(local.evidence_id_pattern, jsondecode(self.body).release_evidence[key])
      ]), false)
      error_message = "Staging release, compatibility and component build evidence IDs are required."
    }
  }
}
