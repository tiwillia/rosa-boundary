# Trusted Actions workload root (PoC)

This root was imported from `rosa-trusted-actions/terraform` at upstream
`1d50516`. It manages only the API workload's regional resources, not the
Boundary VPC, OIDC provider, Bedrock account resources or Boundary task SG.
It remains a **separate Terraform state**; see the [migration report](../../docs/trusted-actions-monorepo-poc.md)
for the existing HCP workspace handoff, certificate/credentials blockers, and
state ownership inventory. Do not apply this PoC to staging or production.

Required inputs include `aws_account_id`, `aws_region`, `vpc_id`, two
`private_subnet_ids` (different AZs), `container_image`, `s3_bucket_name`,
`backplane_url`, `backplane_client_id`, `backplane_client_secret`, and either
`alb_certificate_arn` or `domain_name` plus `route53_zone_id`. Credentials
must not be committed or copied to a persistent Boundary task volume. The
inherited Terraform secret-version provisioning is not approved for rollout.
`public_subnet_ids` remain an inherited compatibility input for `vpc.tf` even
though the ALB is private. Boundary regional's task SG must exist in the
existing VPC and be named `rosa-boundary-<environment>-fargate-sg`.

For offline checks (provider download required):

```sh
terraform fmt -check -recursive deploy/trusted-actions
terraform -chdir=deploy/trusted-actions init -backend=false -input=false
terraform -chdir=deploy/trusted-actions validate
```

Do not use a local plan against staging: HCP provides dynamic credentials and
Git-owned workspace variables through `hcp-terraform/rosa-trusted-actions`.
