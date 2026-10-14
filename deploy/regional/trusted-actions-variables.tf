variable "enable_trusted_actions" {
  description = "Enable the Trusted Actions module ONLY after transferring ownership into regional state; defaults off to avoid duplicate resources."
  type        = bool
  default     = false
}

variable "trusted_actions_container_image" {
  description = "Trusted Actions Quay image pinned by verified digest"
  type        = string
  default     = ""
  validation {
    condition     = !var.enable_trusted_actions || can(regex("@sha256:[0-9a-f]{64}$", var.trusted_actions_container_image))
    error_message = "When enabled, pin Trusted Actions to a verified image digest."
  }
}

variable "trusted_actions_s3_bucket_name" {
  description = "Existing Trusted Actions S3 bucket name for this region; preserve the name during state handoff"
  type        = string
  default     = ""
  validation {
    condition     = !var.enable_trusted_actions || length(var.trusted_actions_s3_bucket_name) > 0
    error_message = "Set the existing bucket name before enabling Trusted Actions."
  }
}

variable "trusted_actions_backplane_url" {
  description = "Backplane API endpoint used by Trusted Actions"
  type        = string
  default     = ""
  validation {
    condition     = !var.enable_trusted_actions || startswith(var.trusted_actions_backplane_url, "https://")
    error_message = "Set an HTTPS Backplane URL before enabling Trusted Actions."
  }
}

variable "trusted_actions_backplane_client_id" {
  description = "Trusted Actions service client ID"
  type        = string
  default     = ""
  validation {
    condition     = !var.enable_trusted_actions || length(var.trusted_actions_backplane_client_id) > 0
    error_message = "Set the Backplane client ID before enabling Trusted Actions."
  }
}

variable "trusted_actions_backplane_client_secret" {
  description = "Inherited secret-version state input; do not set or enable until replaced by reviewed credential design"
  type        = string
  default     = ""
  sensitive   = true
}

variable "trusted_actions_alb_certificate_arn" {
  description = "ACM certificate for the internal API ALB"
  type        = string
  default     = ""
  validation {
    condition     = !var.enable_trusted_actions || startswith(var.trusted_actions_alb_certificate_arn, "arn:aws:acm:")
    error_message = "Set a valid ACM certificate ARN before enabling Trusted Actions."
  }
}

variable "trusted_actions_allowed_accounts" {
  description = "Comma-separated AWS account allowlist for Trusted Actions"
  type        = string
  default     = ""
  validation {
    condition     = !var.enable_trusted_actions || length(var.trusted_actions_allowed_accounts) > 0
    error_message = "Set an explicit account allowlist before enabling Trusted Actions."
  }
}
