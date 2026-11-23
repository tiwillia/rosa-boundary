variable "enable_release_record_bucket" {
  description = "Manage the dedicated staging release-record bucket. Leave enabled for the lifetime of its records."
  type        = bool
  default     = false
  nullable    = false

  validation {
    condition     = !var.enable_release_record_bucket || (var.stage == "stage" && var.aws_account_id == "150100906299" && var.aws_region == "us-east-1")
    error_message = "Release records are supported only in stage, account 150100906299, us-east-1."
  }
}

variable "release_record_publisher_role_arn" {
  description = "Existing dedicated IAM publisher role in the staging account. Required when enabling the bucket."
  type        = string
  default     = ""
  nullable    = false

  validation {
    condition = (
      (!var.enable_release_record_bucket && var.release_record_publisher_role_arn == "") ||
      can(regex("^arn:aws:iam::150100906299:role/([A-Za-z0-9_+=,.@-]+/)*[A-Za-z0-9_+=,.@-]{1,64}$", var.release_record_publisher_role_arn))
    )
    error_message = "Configure an exact staging IAM publisher role ARN; account principals, session ARNs and wildcards are not allowed."
  }
}

locals {
  release_record_bucket_name = "150100906299-rosa-boundary-stage-us-east-1-release-records"
  release_record_bucket_arn  = "arn:aws:s3:::${local.release_record_bucket_name}"
  # IAM matches length, not a hexadecimal alphabet. Publisher and consumer
  # validate the full lowercase Git SHA before constructing a key.
  release_record_arn = "${local.release_record_bucket_arn}/releases/v1/????????????????????????????????????????.json"
}

resource "aws_s3_bucket" "release_records" {
  count         = var.enable_release_record_bucket ? 1 : 0
  bucket        = local.release_record_bucket_name
  force_destroy = false
  tags          = merge(local.common_tags, { Name = local.release_record_bucket_name, Region = var.aws_region })

  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_s3_bucket_versioning" "release_records" {
  count  = var.enable_release_record_bucket ? 1 : 0
  bucket = aws_s3_bucket.release_records[0].id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_public_access_block" "release_records" {
  count                   = var.enable_release_record_bucket ? 1 : 0
  bucket                  = aws_s3_bucket.release_records[0].id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_ownership_controls" "release_records" {
  count  = var.enable_release_record_bucket ? 1 : 0
  bucket = aws_s3_bucket.release_records[0].id
  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "release_records" {
  count  = var.enable_release_record_bucket ? 1 : 0
  bucket = aws_s3_bucket.release_records[0].id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_policy" "release_records" {
  count  = var.enable_release_record_bucket ? 1 : 0
  bucket = aws_s3_bucket.release_records[0].id
  depends_on = [
    aws_s3_bucket_versioning.release_records,
    aws_s3_bucket_public_access_block.release_records,
    aws_s3_bucket_ownership_controls.release_records,
    aws_s3_bucket_server_side_encryption_configuration.release_records,
  ]

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "PublishAndVerifyRecord"
        Effect    = "Allow"
        Principal = { AWS = var.release_record_publisher_role_arn }
        Action    = ["s3:PutObject", "s3:GetObject"]
        Resource  = local.release_record_arn
      },
      {
        Sid       = "DenyOtherWriters"
        Effect    = "Deny"
        Principal = "*"
        Action    = "s3:PutObject"
        Resource  = "${local.release_record_bucket_arn}/*"
        Condition = { ArnNotEquals = { "aws:PrincipalArn" = var.release_record_publisher_role_arn } }
      },
      {
        # A negated exact comparison denies a missing header too. No multipart
        # exemption: these small JSON records use a single signed PutObject.
        Sid       = "RequireCreateOnlyWrite"
        Effect    = "Deny"
        Principal = "*"
        Action    = "s3:PutObject"
        Resource  = "${local.release_record_bucket_arn}/*"
        Condition = { StringNotEquals = { "s3:if-none-match" = "*" } }
      },
      {
        Sid       = "RequireSSES3"
        Effect    = "Deny"
        Principal = "*"
        Action    = "s3:PutObject"
        Resource  = "${local.release_record_bucket_arn}/*"
        Condition = { StringNotEquals = { "s3:x-amz-server-side-encryption" = "AES256" } }
      },
      {
        Sid       = "DenyDeletionAndReplication"
        Effect    = "Deny"
        Principal = "*"
        Action    = ["s3:DeleteObject", "s3:DeleteObjectVersion", "s3:ReplicateObject", "s3:ReplicateDelete"]
        Resource  = "${local.release_record_bucket_arn}/*"
      },
      {
        # S3 lifecycle expiration does not consult object-level delete denies.
        Sid       = "DenyLifecycleExpirationConfiguration"
        Effect    = "Deny"
        Principal = "*"
        Action    = "s3:PutLifecycleConfiguration"
        Resource  = local.release_record_bucket_arn
      },
      {
        Sid       = "DenyNonTLS"
        Effect    = "Deny"
        Principal = "*"
        Action    = "s3:*"
        Resource  = [local.release_record_bucket_arn, "${local.release_record_bucket_arn}/*"]
        Condition = { Bool = { "aws:SecureTransport" = "false" } }
      },
    ]
  })
}

output "release_record_bucket_name" {
  description = "Dedicated release-record bucket name; null when disabled."
  value       = one(aws_s3_bucket.release_records[*].bucket)
}
