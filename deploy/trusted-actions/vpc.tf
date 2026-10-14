# This child module consumes Boundary's network; it never creates a second
# VPC or subnets. Former standalone VPC resources require a separate,
# non-destructive state release if they are present in the old workspace.
locals {
  vpc_id           = var.vpc_id
  private_subnet_a = var.private_subnet_ids[0]
  private_subnet_b = var.private_subnet_ids[1]
}
