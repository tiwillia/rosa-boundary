# Ephemeral Boundary and Trusted Actions development environment

This workflow deploys Boundary and Trusted Actions (TA) into **one explicitly selected AWS
account and the same region**, with a shared VPC and five separately owned
Terraform states. It is not the [HCP staging delivery workflow](../staging-deployment.md).

**No cluster ID or existing cluster is required.** Deployment creates the
environment, not a managed cluster, investigation, or Trusted Action execution.
Any later investigation or cluster action needs its own approved test scope.

## Scope and ownership

| State | Source root | Owns |
| --- | --- | --- |
| Boundary account | `deploy/account/` | Boundary IAM/OIDC identities and account configuration |
| Boundary network | `deploy/network/` | Shared VPC, subnets, routing and NAT |
| Boundary regional | `deploy/regional/` | Fargate task template, Lambdas, EFS, content and audit storage |
| TA global | `components/trusted-actions/terraform/global/` | TA IAM and Secrets Manager container; optional DNS validation |
| TA regional | `components/trusted-actions/terraform/regional/` | Internal ALB, EC2/ECS service, SQLite EBS volume, storage and logs |

[`scripts/ephemeral_env.py`](../../scripts/ephemeral_env.py) is the small CLI entry
point; implementation lives in `scripts/ephemeral_environment/`.
The root Makefile provides `ephemeral-env`, `ephemeral-env-status` and
`ephemeral-env-destroy`, plus local-only `ephemeral-env-reset`. Recovery uses
the direct Python command below.
Keep all five private states together; never substitute staging state, adopt
another environment's resources, or let TA read shared HCP remote state.

Keycloak is **external**: provide an approved existing issuer and client. This
workflow neither installs Keycloak nor creates users, clients, realms or groups,
and destruction must not affect the identity provider. There is no shared
internal Keycloak URL or contributor account as a development default.

Inventory existing IAM OIDC providers and account/region-wide Bedrock invocation
logging. Reference an existing primary Keycloak IAM provider explicitly; this is
an AWS trust registration, not the external Keycloak server. Preflight refuses a
competing Bedrock logging owner; choose another approved account/region rather
than overwrite it.

## Prerequisites and missing credentials

- An approved personal AWS account and explicit common region, verified against
  STS identity. The AWS profile defaults to `default`; override it when needed.
- Git, Make, Python 3, Terraform compatible with Boundary's current constraint,
  Podman, Go and `uv`; use [local setup](contributor-local-setup.md) and current
  module/tool pins. Image builds need registry/network access and approved GitHub
  build authentication; do not put tokens on command lines.
- Permission to create the reviewed IAM, networking, storage, ECS/EC2, Lambda,
  ECR, logging, KMS and budget resources. Account budget notifications are not
  spending limits. Managed S3 Files availability and quotas must support the
  chosen region.
- External Keycloak issuer with the client, group/role and AWS principal-tag
  mappings described below. Defaults match the `tsw-boundary` development realm;
  verify these mappings for another issuer. IAM retrieves the CA thumbprint when
  creating its OIDC provider; no manual fingerprint is required. `username` and
  `uuid` are not interchangeable. A non-admin SRE login is needed for later access.
- Vault CLI installed and authenticated by your approved process. Provide a
  readable secret reference and set `VAULT_ADDR` or configure `vault.address`.
  Follow [Log in to Vault](#log-in-to-vault) before deployment.
  The secret must use the fixed field names documented below.
  There is **no default internal Vault address or organization-specific path**.
- One provisioned **service account for OCM integration and Backplane**:
  its client ID and client secret accessible through Vault, with approved
  integration AMS and Backplane privileges. This workflow treats client IDs as private values,
  not plaintext application configuration. TA uses
  `https://api.integration.openshift.com`. OCM environments share production
  SSO, so caller-token JWKS defaults to its public signing keys. This is
  distinct from Boundary's Keycloak identity.
- TA Backplane HTTPS base URL in the same Vault secret.
  These inputs are required for normal TA startup even when no cluster action is run.
  The workflow uses the same service account for OCM and Backplane; confirm
  that it has the required permissions for both.
- **Optional TLS:** an already issued ACM certificate in TA's account/region
  covering a private hostname you choose. Arrange issuance separately; the tool
  does not request a certificate. Omit `ta_tls` to use private HTTP without any
  certificate or custom domain.

> **Dev-only HTTP default:** TA's load balancer remains internal and accepts
> traffic only from its VPC, but bearer tokens and responses cross that network
> **unencrypted**. Authentication stays enabled. Use only an approved disposable
> development environment, never customer data. Configure TLS below when
> encryption is required. Keycloak, Vault, OCM and Backplane still require HTTPS.

**Missing credentials block deployment.** The tool does not provision service
accounts or Vault secrets. Obtain approved references; do not invent paths,
borrow another environment's credentials or disable authentication.

Bedrock model activation, first-use forms and paid inference are separate
approvals. They are not prerequisites for creating the environment without an
agent inference test; see [model access](bedrock-model-access.md).

### Log in to Vault

The deployment reads your Vault secret using the Vault CLI. Configuring
`vault.address` and `vault.path` does **not** log you in. A missing or expired
login blocks deployment before image publication and AWS resource creation.

1. Point the interactive CLI at the **same HTTPS address as `vault.address`**
   in your private config (or the `VAULT_ADDR` used when that field is omitted):

   ```bash
   export VAULT_ADDR="https://your-approved-vault-server"
   ```

2. If an expired `VAULT_TOKEN` is exported, remove that stale override first:

   ```bash
   unset VAULT_TOKEN
   ```

   `VAULT_TOKEN` takes precedence over the CLI token helper, so a stale exported
   token can mask a successful new login. Do not unset a valid token supplied
   intentionally by your approved authentication process.

3. Use your organization's approved login method. For a server with OIDC
   browser login enabled at the default mount:

   ```bash
   vault login -no-print -method=oidc
   ```

   Complete the browser login. If your administrator specifies a different auth
   mount or role, supply those settings, for example
   `-path=<approved-auth-mount> role=<approved-role>`. OIDC is an example, not a
   guarantee that your server enables this method.

   Alternatively, if your approved process gives you a Vault token:

   ```bash
   vault login -no-print
   ```

   Enter it at the hidden prompt. Never append the token to the command or put
   it in your deployment JSON, shell history or repository. `-no-print` suppresses
   the login response; the CLI still stores the login in its configured token
   helper (normally `~/.vault-token`).

4. Verify the login without printing token details:

   ```bash
   vault token lookup -format=json >/dev/null
   ```

   Then retry `make ephemeral-env`. The deployment uses the saved CLI login or
   an intentionally supplied `VAULT_TOKEN`; it never performs login for you.
   No environment reset is needed after this Vault-read failure.

If login works but deployment still cannot read the secret, verify policy
access to the exact `vault.path` in your config, including `/data/` for KV v2.
An inability to look up a token can also be a policy restriction; consult the
Vault administrator. Check connectivity and CA trust for connection/TLS errors
rather than disabling certificate verification. Do not print or share a raw
`vault read` response while troubleshooting.

## Private configuration

Keep JSON configuration and environment state outside the checkout and
cloud-synced/shared directories, with owner-only permissions. Unknown config
fields are rejected. Review network CIDRs, two usable AZs and NAT costs.

The default configuration file is `ephemeral-dev.json` in Boundary's config
directory: `~/.config/rosa-boundary/ephemeral-dev.json` by default, or under
`$XDG_CONFIG_HOME/rosa-boundary/` when set. Make targets use this path automatically;
`EPHEMERAL_CONFIG` is only needed to override it. The export below is optional
for Make, but lets you reuse the path in editor and direct Python commands.

```bash
export EPHEMERAL_CONFIG="${XDG_CONFIG_HOME:-$HOME/.config}/rosa-boundary/ephemeral-dev.json"
mkdir -p "$(dirname "$EPHEMERAL_CONFIG")"
chmod 700 "$(dirname "$EPHEMERAL_CONFIG")"
umask 077
```

Save the JSON below at that path using your editor, then ensure the file is
owner-readable/writable only with `chmod 600 "$EPHEMERAL_CONFIG"`.
Terraform state remains in the separate state directory described below.

Minimal JSON structure (replace **every** illustrative account, endpoint,
secret reference and policy choice before deployment):

```json
{
  "account_id": "123456789012",
  "region": "us-east-1",
  "keycloak": {
    "issuer_url": "https://keycloak.example.invalid/realms/development"
  },
  "vault": {
    "address": "https://vault.example.invalid",
    "path": "example-kv/data/development/ta"
  },
  "boundary_account": {
    "bedrock_budget_notification_email": "developer@example.com"
  }
}
```

### Defaults and overrides

Defaults apply only to omitted keys; explicit invalid values, including `null`
and empty strings, are rejected rather than silently replaced. Documented empty
lists and optional empty objects remain valid.

| Optional setting | Default |
| --- | --- |
| `profile` | `default` (does not inherit `AWS_PROFILE`) |
| Make `EPHEMERAL_CONFIG` | `${XDG_CONFIG_HOME:-$HOME/.config}/rosa-boundary/ephemeral-dev.json` |
| Make `EPHEMERAL_NAME` | Current account name from `id -un` (Linux/macOS); independent of `$USER` |
| `state_dir` | `$XDG_STATE_HOME/rosa-ephemeral`, or `~/.local/state/rosa-ephemeral` when unset/empty |
| `keycloak.client_id` | `rosa-boundary-sre` |
| `keycloak.required_groups` | `["ai-sd-sre", "eng-openshift-enterprise", "sre-team"]` (membership in any one allows Lambda access) |
| `keycloak.required_oidc_role` | `ai-sd-sre` |
| `keycloak.abac_tag_key` | `username`; override with `uuid` for issuers using that mapping |
| `keycloak.thumbprint` | Omitted: AWS IAM retrieves the top intermediate CA thumbprint when creating the provider; an explicit SHA1 override is supported |
| `vault.address` | `VAULT_ADDR`; one of these must be supplied |
| `ocm` | Integration API and production SSO JWKS; the entire block may be omitted |
| `ta_tls` | Omitted: HTTP using the internal ALB's AWS DNS name; no custom DNS or certificate required |
| `boundary_account.bedrock_monthly_budget_usd` | USD 25/month notification threshold, **not a spending cap** |
| `ta_app_config` | Optional; omitted or `{}` uses the defaults below |
| `ta_app_config.roles` | Only `SREP`, mapped to `BackplaneOsdSrepResource` |
| `ta_app_config.actions.aws_allowed_regions` | The selected deployment region only |
| `ta_app_config.actions.aws_allowed_operations` | Empty: AWS mutations disabled |
| `ta_app_config.actions.allowed_namespaces` | Empty: permissive Kubernetes authorization, matching stage; see caveat below |
| `ta_app_config.actions.allowed_secrets` | Empty: only restricts Secret requests when the namespace allowlist is nonempty |
| `ta_app_config.workers` | TA runtime defaults: 4 workers, 5s poll interval, 2m execution timeout |
| `network` | Terraform defaults: `10.80.0.0/20`, two region-specific AZs and a single NAT gateway |

Account/region, identity issuer, Vault secret path and
budget recipient remain explicit. There is no safe universal default for those
choices. Relative `XDG_STATE_HOME` paths are rejected. Effective values are
resolved before saving environment configuration and checking ownership.

### Optional TA HTTPS

Add this block to the top-level JSON to use TLS instead of the private HTTP
default:

```json
"ta_tls": {
  "internal_fqdn": "ta.dev.example.com",
  "certificate_arn": "arn:aws:acm:us-east-1:123456789012:certificate/00000000-0000-0000-0000-000000000000"
}
```

Replace both values. The certificate must be `ISSUED`, in the selected account
and region, and cover the hostname. Deployment creates a VPC-private DNS zone
and record pointing that name at the internal ALB. Use a name under a domain
you control and a certificate trusted by your clients. Explicit incomplete or
invalid `ta_tls` blocks fail validation; TLS failures never fall back to HTTP.
Changing an existing environment's configuration requires a new environment name,
unless its unused local record passes the reset checks below.

### Identity and authorization choices

Replace all placeholders. Optional `ocm.base_url` defaults to
`https://api.integration.openshift.com`; optional `ocm.jwk_cert_url` defaults to
`https://sso.redhat.com/auth/realms/redhat-external/protocol/openid-connect/certs`,
the production SSO signing keys used across OCM environments. An explicit HTTPS
override must validate the intended caller tokens, not arbitrary Keycloak tokens.
This does not enable non-integration OCM API targets in this workflow.
The Keycloak client, groups, role and ABAC defaults match the existing
`tsw-boundary` dev configuration; they do not configure the external realm.
Override them if your issuer uses different mappings. The issuer URL has no
default, and no account-specific IAM provider ARN is defaulted.
If an existing environment omitted `abac_tag_key` when the default was `uuid`,
set it explicitly to `uuid` to preserve that environment's configuration hash;
do not edit ownership metadata to adopt the new default.
`required_groups` controls Lambda access;
`required_oidc_role` and `abac_tag_key` must match the issuer's AWS session tags.
The `actions` block and both Kubernetes allowlists are optional. By default,
`allowed_namespaces` and `allowed_secrets` are empty, matching stage. An empty
namespace list enables TA's permissive Kubernetes authorizer, **including secret
requests**, regardless of `allowed_secrets`. Set a nonempty namespace allowlist
to restrict namespaced requests; Secret requests then also require an explicit
`allowed_secrets` entry. Keep AWS mutation operations empty unless an approved
test needs them.

The entire `ta_app_config` block is optional. Its `roles` setting maps TA roles
to AMS resources and defaults to only:

```yaml
roles:
  - id: SREP
    amsResource: BackplaneOsdSrepResource
```

This uses the first mapping from stage, not its other two roles. It does not
grant AMS permissions; callers must already pass the AMS access review.
Explicit role lists replace the default and retain their order (first match
wins). Explicit empty or invalid role lists are rejected, not defaulted.
Workers and action settings can also be omitted.

Store **references, not credential values**, in this file. The selected Vault
secret requires these three fields:

| Vault key | Value |
| --- | --- |
| `trusted_actions_sa_client_id` | Service-account OAuth client ID used for OCM integration and Backplane |
| `trusted_actions_sa_client_secret` | The same service account's OAuth client secret |
| `trusted_actions_backplane_url` | Backplane HTTPS API base URL, distinct from the OCM API URL; no embedded credentials, query or fragment |

These field names are fixed and cannot
be overridden in configuration.
The script supplies that one pair to both of TA's separately named OCM and
Backplane runtime settings. There are no separate Backplane credential selectors.
`ta_app_config.backplane.client_id` and `ta_app_config.backplane.base_url`
are not accepted. The `backplane` app-config block may be omitted.
For KV v1 versus v2,
use the reference syntax expected by the tool's Vault CLI command: a raw
`vault read` path for KV v2 includes its `/data/` API segment, whereas a KV v1
path does not. The tool accepts the corresponding `data` or nested `data.data`
response. Confirm the mount version with the secret owner;
never print the response to discover them.

TA client IDs, secrets and Backplane URL are read through the Vault CLI and populated by the
script in the owned Secrets Manager secret, outside Terraform. A private
regional override supplies ECS `secrets` selectors for both client IDs and the
Backplane URL as well as the existing secret/token entries; the S3 application
config and ordinary task-definition environment contain none of these values.
A generated local override
disables `aws_secretsmanager_secret_version.app` only in the private TA global
working copy; the required `backplane_client_secret` input receives an explicit
nonsecret placeholder. Shared Terraform keeps its existing behavior.
Real client IDs/secrets and the private Backplane URL must never enter tfvars,
plans/state, source snapshots, image
contexts, manifests, shell history, logs, Boundary's EFS home or audit escrow.

Optional `keycloak.existing_provider_arn` references an already managed primary
IAM OIDC provider for that issuer/account, with matching client audience. The
script generates a local Terraform override to disable creation and wire trust
and outputs to that ARN; it does not add a shared Terraform create/reference
input. Optional `boundary_account.reference_existing_github_oidc` references an
existing GitHub provider. References are not imports or ownership transfers;
cleanup must leave these providers intact.

For a newly owned provider, the private override omits `thumbprint_list` unless
`keycloak.thumbprint` is supplied. [IAM retrieves the CA thumbprint automatically](https://docs.aws.amazon.com/IAM/latest/UserGuide/id_roles_providers_create_oidc_verify-thumbprint.html);
this does not disable certificate validation or pin the rotating leaf certificate.
Referenced providers retain their existing thumbprints under their original owner.
IAM discovery happens at provider creation, not during offline config loading
or on every redeploy. Removing a previously explicit thumbprint does not force
IAM to refresh it; config changes still require a new environment name.

The tool supplies explicit local ownership/wiring. Do not copy a root `.env`,
local `terraform.tfvars`, old override file or HCP configuration into its private
working copy. For the manual component-only workflows, variable precedence is
different; do not mix those instructions with this one.

## Select source and deploy

**Run only trusted, reviewed source.** Selected Makefiles, build helpers and
Terraform execute on the host; snapshots are not a sandbox. Use supported build
secret mounts, never Vault credentials or AWS runtime secrets in builds.

Run from the repository root. Inspect the current CLI before use:

```bash
python3 scripts/ephemeral_env.py --help
```

Make finds the default config path automatically and defaults `EPHEMERAL_NAME`
to the current account name from `id -un`, not the potentially stale `$USER`
environment variable. With the config saved at its default location, run:

```bash
make ephemeral-env
```

Optionally set `EPHEMERAL_CONFIG` for a different private JSON path, or
`EPHEMERAL_NAME=timdev` for a different deployment name. The name identifies one
deployment and scopes its AWS resource names and private state directory.
Reuse it for redeploy, status and destroy; choose a different name for a separate
environment. Names must be 1–18 lowercase letters/digits/hyphens, starting with
a letter; generated S3 bucket names may impose a shorter regional limit.
Account names are not silently normalized or truncated: if yours fails these
limits, supply an explicit valid name. If account lookup fails, an explicit
name is required. When using an override, keep supplying it to all lifecycle targets.

The optional `create` action freezes source offline without deployment:

```bash
python3 scripts/ephemeral_env.py create \
  --config "$EPHEMERAL_CONFIG" --name "${EPHEMERAL_NAME:-$(id -un)}"
```

Config validation still applies; offline creation does not authenticate or
verify external references. A later deploy selects source again. Changing config
requires a different name or a verified local-only reset of an unused record,
not hand-editing the ownership record.

### Early failures and local configuration records

The first deploy attempt saves local ownership metadata and empty Terraform
states **before** AWS preflight. A local record is not evidence that the tooling
created the manually deployed resources already in your account.

- **Existing OIDC provider collision:** IAM OIDC providers are account-wide;
  changing region does not help. Verify the existing provider is approved for
  reuse and use the explicit reference setting shown in the error. References
  leave ownership with the original deployment; do not delete shared providers
  merely to pass preflight.
- **Ownership/configuration mismatch:** config is bound to the local record,
  even after a failed first preflight. The error identifies the record, original
  config path and changed sections without displaying private values. Restore
  the original configuration to continue with that record, reset a verified
  unused record as below, or choose a new `EPHEMERAL_NAME` for intentional config
  changes. Deleting AWS resources does not clear local metadata. Preserve old
  records/states for any cleanup; do not delete them or edit their hashes.

### Reset an unused local record

After an offline create or failed preflight that never reached AWS mutation:

```bash
make ephemeral-env-reset
make ephemeral-env
```

Reset uses the same default config path and username as deploy; pass the same
`EPHEMERAL_NAME` override if you used one. Your current config may contain a new
region or changed defaults; you do not need to restore the original config.
Keep the same `state_dir` so reset can locate the old record. The supplied config
file must be outside the environment directory.

Reset takes the environment lock, verifies the original ownership/config record
and all five unchanged initial empty states, and refuses any apply attempt,
image/repository history, cloud-operation journal, secret/service update record,
Terraform lock or unverified state. It requires interactive confirmation (or
explicit `EPHEMERAL_ARGS=--yes` for this local-only operation). It invokes **no
AWS, Vault, Terraform, image builds or selected-source commands**.

The unused record is moved, not deleted, into a private
`<state_dir>/.reset-archives/<name>-<unique-id>/record/` directory. Its receipt is
`reset.json` alongside `record/`. The original environment directory and lock
stay in place for concurrency safety; the next deploy creates a fresh ownership
record under the same name. Reset does not deploy anything or remove AWS
resources. A repeated reset with no active record is a no-op.

If reset refuses, preserve the evidence and use explicit recovery or AWS destroy
with the original configuration, as appropriate. An interrupted local archive
also fails closed: retain both the active directory and partial archive for
investigation rather than deleting metadata to force a retry.

The following commands publish images and create AWS resources. Obtain approval
of the account, region, source and scope first:

```bash
# Snapshot the current working tree, including eligible untracked changes.
make ephemeral-env

# Alternatively: deploy an exact local commit, initially or on redeploy.
make ephemeral-env \
  EPHEMERAL_COMMIT="$APPROVED_COMMIT_SHA"
```

Source snapshots identify the bytes being deployed. Dirty mode captures
working-file bytes, not just HEAD or the Git index; deleted files stay deleted.
Review eligible untracked files first. Standard exclusions are defense in
depth, **not a secret scanner**: credentials disguised as ordinary source can
still leak into a build. Commit mode uses the selected commit, not local edits.
Neither mode should switch/reset/stash the user's checkout.

The selected revision must contain supported Terraform/Make inputs; do not mix
infrastructure from HEAD with application source from another commit.

Review the recorded source fingerprint and image architectures/digests. Build
and publication, Terraform plan/apply, and service readiness are distinct
boundaries. Review each complete private plan, IAM changes, deletions,
replacements, audit effects and costs. Each invocation requests **one approval
for the entire stated operation**, not one per Terraform root or cleanup step.
After approval, plan summaries and cleanup details are still printed and validated,
but execution proceeds without further prompts. Approval does not carry over to
a retry or a later command. Account, ownership and plan-integrity failures still
abort; approval does not bypass these checks. Do not use `--yes` merely to bypass
a missing terminal. The optional
`EPHEMERAL_ARGS="--yes --account-id $APPROVED_ACCOUNT_ID"` requires the explicit
account to match both config and STS; use only for separately authorized scope.

Terraform resource lifecycle progress, including repeated "Still destroying..."
messages, is displayed while commands run. Raw plan values, build output and
credential-bearing command responses are not streamed. Complete failed-command
diagnostics remain in the private log file identified by the error; full plans
remain in their private review files.

Apply order: Boundary account → Boundary network → TA global → TA regional →
Boundary regional. Both regional roots share the network. Without `ta_tls`,
Boundary's `rta_server_url` uses `http://` plus TA's internal `alb_dns_name`.
With TLS, it uses TA's `internal_fqdn` output, which already includes `https://`.
The tool builds separate `linux/amd64` images and three Boundary Lambda ZIPs;
Boundary bundles `rta` from the same source snapshot. Record digests, not `latest`.
Both image Makefiles expose `build-amd64` with `IMAGE_NAME` and `TAG`.

## Inspect and maintain

```bash
make ephemeral-env-status
```

All ephemeral make targets use concise human-readable summaries by default.
Approvals separate the target, scope, warnings and cleanup details with headings
and blank lines. Terraform plan summaries show action counts; full plans remain
in their private review files.

To inspect the full recorded result, use:

```bash
make ephemeral-env-status EPHEMERAL_ARGS="--json"
```

`--json` selects the detailed final result for other actions too; their approval
and progress messages still appear normally. Status performs no cloud checks.

Status shows local provenance and root checkpoints, not fresh AWS health, login
or audit evidence. Detailed image references and connection settings are available
with `--json`. Preserve private states, generation
directories and journals; do not edit fingerprints. Redeploy captures the current
tree or selected commit in an immutable generation, retaining the environment's
state paths. After a partial update, `active_generation`, individual applied
generations and `outputs_generation` may differ; old outputs do not prove the
selected source is deployed everywhere.

Inspect AWS control-plane resource/service status under the approved profile and
region. TA's endpoint is private: use the shared VPC or a separately authorized
private access path. Merely knowing the URL does not make it laptop-accessible.
No investigation or cluster action is needed to record deployment readiness.

Redeploy uses the same command/name, with `EPHEMERAL_COMMIT` set or omitted.
Review source and saved plans again. Updates require no active Boundary tasks or
unclosed investigations; arrange approved closure rather than bypass the guard.
Redeploy rereads Vault and restarts TA to pick up the populated secret.

After a partial failure, retain the record and states, inspect status and follow
the recovery procedure below. Do not automatically import, delete unknown
resources or keep applying unexplained changes.

Deployment creates or updates a named CLI profile matching `EPHEMERAL_NAME` in
your regular `${XDG_CONFIG_HOME:-$HOME/.config}/rosa-boundary/config.yaml`.
Other named profiles and an existing active selection are preserved; the first
profile becomes active only if there was no active selection. The automation
builds its profile-registration helper from the selected source and uses
`config init --auto-discover=false --overwrite --yes` against a private staged
copy, without hand-parsing your YAML. Before atomically replacing the real config,
it checks the proposed document with `rosa-boundary config list-profiles` using
the installed CLI on `PATH`, if present. If that CLI cannot read the source's
schema (for example, an older CLI that does not recognize `ocm_url`), registration
fails without changing your config. Run `make install-cli` from the selected
source and retry; this local repair does not require an AWS redeployment.
Invalid or symlinked config files are refused rather than overwritten.
No authentication or AWS calls are performed by profile registration.

Use a current CLI with the explicit profile name, for example:

```bash
make install-cli
rosa-boundary --profile tiwillia session list
```

Replace `tiwillia` if you chose another environment name. No XDG configuration
or cache overrides are needed. The automation does not install or replace your
workstation CLI. `connection.json` records `cli_profile`, `cli_config_path` and
the corresponding command, not login tokens or proof of connectivity.
Older deployments may have unused `client-config/` and `client-cache/`
directories under their state directory; these are left intact, but new profile
registration does not use them. If you previously exported XDG overrides for
those directories, restore your normal shell's XDG settings before deploying
or using the CLI.

For later interactive use, follow the [user access guide](user-access-guide.md).
Cluster access, OCM user login, Backplane trusted egress/proxy and action
permissions are later test inputs.

`RTA_SERVER` is the server origin, **without** `/api/v0/trusted-actions`.
HTTP dev tasks also receive `RTA_ALLOW_INSECURE_HTTP=true`; `connection.json`
records the same client setting for separately authorized private access.
The `rta` client otherwise refuses authenticated non-loopback HTTP. Manual use
can opt in with `--allow-insecure-http`; it warns on stderr, and HTTPS-to-HTTP
redirects remain forbidden even when opted in. HTTPS deployments do not enable
this exception. Persistent/stage/production Boundary stacks still require HTTPS.
`rta` authenticates using the task-scoped OCM configuration or an explicit
`--token`; it does **not** support an `RTA_TOKEN` environment variable. Never put
the TA service-account secret into the user's OCM config to make a client work.

## Recover an interrupted operation

After verifying the original process has exited, inspect the private status,
saved plans and journals. With explicit recovery approval:

```bash
python3 scripts/ephemeral_env.py recover \
  --config "$EPHEMERAL_CONFIG" --name "${EPHEMERAL_NAME:-$(id -un)}"
```

Recovery reconciles journaled interrupted Terraform applies and ECR operations
against saved-plan hashes, generation, state lineage/serial and ownership.
Unverified image publication requires a later rebuild with a fresh immutable tag.
It is not rollback, does not finish deployment or adopt unknown resources, and
does not prove readiness. Inspect status and approve the next deploy or destroy.

Do not edit state hashes/lineages, delete journals, import orphaned resources or
blindly remove a Terraform lock. The tool refuses recovery while a state lock
remains; independently establish that its owning process has stopped and obtain
approval for any explicit stale-lock resolution. An unexplained state mismatch
is a blocker, not permission to adopt it.

## Destroy and account for retained resources

**There is no automatic environment TTL or scheduled destruction.** Session
timeouts/reapers do not remove the VPC, NAT, TA service, EC2/EBS or other standing
infrastructure. It continues to cost money until explicitly destroyed.

After explicit cleanup approval:

```bash
make ephemeral-env-destroy
```

The single destroy approval covers consumer cleanup, every owned Terraform root,
owned ECR repositories/images and owned Bedrock invocation payloads. Destroy plans remain visible but do not ask
for approval again. Consumers must go before their identities/network: TA
regional before TA global, Boundary regional before account/network teardown.
Only delete resources/state identified as owned by this environment. External
Keycloak, Vault data, OCM service accounts and unrelated AWS resources are out
of scope.

If Boundary sessions or investigations remain, destroy lists their tasks, access
points and generated task definitions before asking for operation-wide approval.
Approving terminates the listed sessions, waits for tasks to stop (allowing normal shutdown),
deregisters investigation task definitions, and deletes their access points
before proceeding to the Terraform destroy plans without additional prompts.
This applies only to the environment's state-owned cluster and filesystem, whose live owner tags are
rechecked. Terraform-managed access points remain for Terraform to delete.
Declining stops destruction without stopping sessions or deleting resources.
If new consumers appear during confirmation, rerun destroy to review them;
do not launch new sessions while destroying the environment.

**Export anything you need before approving cleanup.** Workspace data and
dev-only audit storage are subsequently deleted by destroy; waiting for task
shutdown does not prove successful audit sync or preserve evidence.
Noninteractive `--yes --account-id <matching-account>` also approves this printed
consumer cleanup, not just Terraform plans.

Destruction still refuses unresolved interrupted ECR journals. Resolve the
recorded interruption first. Each root is destroyed using its own applied source
generation, so a partially deployed newer
generation must not make cleanup use the wrong Terraform source. Destroy retains
the private states, generation history and ownership metadata for evidence;
`destroyed` is not proof of an empty AWS account, and that name is not reusable.

The **dev-only** `ephemeral_environment = true` mode permits removal of owned
Boundary audit/content and TA bucket data. Boundary uses one-day container-log
retention, preserves 90-day session/Bedrock logs, and omits default COMPLIANCE
retention on its audit bucket. TA keeps Object Lock capability,
uses existing `enable_worm = false` to omit default retention, and enables bucket
`force_destroy`. TA application/Firehose log retention remains 30/90 days.
Never use ephemeral storage for customer data/compliance evidence, weaken
persistent defaults, or bypass existing object retention.

Bedrock logging uses the standard regional configuration, not a development
disable switch. Its account/region-wide scope includes text invocations outside
Boundary, so review payload sensitivity and readers before deployment.
Deployment itself runs **no inference**. Never overwrite or delete another
owner's logging configuration during deployment, recovery or cleanup.

**Bedrock payload deletion is explicit, not a weakened Terraform default:**
the S3 bucket keeps `force_destroy = false`, and its KMS key has a 30-day deletion
window. Ephemeral destroy verifies the bucket's exact identity in hash-checked
state, expected account, live owner tags and region, then includes irreversible
payload removal in the operation-wide approval. This may delete model
input/output evidence from **outside Boundary** because invocation logging is
account/region-wide. Export anything required before approving.

Cleanup disables only the verified owned logging configuration, drains object
versions and delete markers and aborts unfinished uploads, then lets Terraform
delete the bucket normally. Object keys and payloads are not printed. Object
retention, legal holds, ownership failures and permission errors are not bypassed.
A failed destroy caused by `BucketNotEmpty` can be retried with the same make
command; no source-snapshot, state-hash or shared Terraform edits are needed.

Check running tasks, EFS access points, versioned/retained objects, pending task
definition deletion and service-linked leftovers. The ECS Exec KMS key uses a
seven-day deletion window. TA's secret requests immediate deletion
(`recovery_window_in_days = 0`), which can still be asynchronous. Record residual
resources, identifiers, deadlines and follow-up work. Do not claim complete
teardown or an empty account while persistent buckets or pending deletions remain.

## Verification boundaries

The deployment verifier checks TA ECS stability, image references and the three
active Boundary Lambda ZIP hashes. These infrastructure checks do not prove
private API connectivity. Recorded status is not a fresh verification.

Environment deployment does not prove OIDC/ABAC enforcement, OCM integration
authorization, Backplane cluster access, AWS action credential chaining, Bedrock
inference, S3 Files visibility or audit delivery. Local mocks/plans cannot prove
those live behaviors either. TA's request audit, action-level logger and
CloudWatch/Firehose delivery are different surfaces; Boundary's ECS Exec audit
does not establish TA audit coverage.

**Deployment account is not action-target confinement.** TA currently loads
`allowed_accounts` but does not enforce it as an action-target account guard.
Backplane resolves target credentials; AMS authorization and region/action
policies are separate controls. Deploying both components in one account does
not prove that later TA actions cannot reach another account.

Use [Boundary live verification](../../.agents/skills/boundary-live-verification/SKILL.md)
for an approved specific test, and TA's
[testing guide](../../components/trusted-actions/TESTING.md) for its separate module.
Report missing Vault/service-account credentials, TLS inputs and live-user
authentication separately. Image build/publication also needs its own evidence;
offline tests or a deployment plan do not establish a successful image build.
