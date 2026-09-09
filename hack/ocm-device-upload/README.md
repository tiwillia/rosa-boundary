# OCM credential injection PoC

This proof of concept obtains a short-lived OCM access token on the local
machine, then writes an **access-token-only** OCM configuration to an existing
`rosa-boundary` ECS task through ECS Exec stdin. It neither saves nor uploads a
refresh token.

## Prerequisites

- Go 1.25 or later.
- Browser access to Red Hat SSO on the local machine.
- A running investigation task and a working `rosa-boundary` configuration.
- `rosa-boundary` and `session-manager-plugin` available locally. The PoC
  invokes `rosa-boundary join-task`, which uses the existing AWS/OIDC and ECS
  Exec implementation.

## Run

From this directory, run:

```bash
go run . --task-id d9bc357412a24631b140408ce0aceea3
```

The default `--flow=auth-code` starts a loopback callback listener at
`127.0.0.1:9998`, opens the Red Hat SSO login page in a browser, and waits for
the browser redirect. This avoids the device-code approval page when the SRE is
already using the local workstation. Red Hat SSO may still require login or
other normal browser interaction.

The callback is protected by a random OAuth state value and PKCE S256. The
token exchange uses Go's default verified-TLS HTTP client; it does not reuse
the OCM CLI implementation's TLS-bypassing auth-code helper.

To use the device flow instead, for example from a machine where the loopback
callback is impractical, pass `--flow=device`:

```bash
go run . --flow=device --task-id d9bc357412a24631b140408ce0aceea3
```

The device flow prints the Red Hat SSO verification URL, attempts to open it in
a browser, and polls until authorization completes. Both flows report expiry
metadata only; neither prints an access or refresh token.

The PoC normally invokes the installed `rosa-boundary` binary. To run the
repository CLI without installing it, pass its command line explicitly:

```bash
go run . \
  --task-id d9bc357412a24631b140408ce0aceea3 \
  --rosa-boundary-command='go run ./cmd/rosa-boundary' \
  --rosa-boundary-directory=../..
```

The directory flag is necessary because this proof of concept is a separate Go
module. The existing CLI can also be run directly from the repository root:

```bash
go run ./cmd/rosa-boundary join-task d9bc357412a24631b140408ce0aceea3
```

The `--rosa-boundary-command` value is a local command line, not a shell
expression. Quote it when it has multiple words; shell operators and quoted
subarguments are not supported.

Configuration for the invoked `rosa-boundary` command comes from its normal
flags, environment variables, and config file. In particular, make sure its
SRE role, ECS cluster, and AWS region are configured before running this PoC.

## What the task receives

The receiver runs as `sre`, disables terminal echo before accepting one
base64-encoded input line, and atomically writes
`/home/sre/.config/ocm/ocm.json` with mode `0600`. Its complete JSON shape is:

```json
{
  "access_token": "<token>",
  "client_id": "ocm-cli",
  "scopes": ["openid"],
  "token_url": "https://sso.redhat.com/auth/realms/redhat-external/protocol/openid-connect/token",
  "url": "https://api.openshift.com"
}
```

The receiver runs `ocm whoami` before reporting success. It emits `OK` plus an
internal success marker that the PoC detects despite ECS Exec terminal framing.
A missing success marker means the upload is treated as failed.

## Validate the PoC

After a successful run, join the task separately and verify the injected
credential:

```bash
rosa-boundary join-task d9bc357412a24631b140408ce0aceea3
ocm whoami
stat --format='%U %a %n' /home/sre/.config/ocm/ocm.json
jq 'keys' /home/sre/.config/ocm/ocm.json
```

Expected results:

- `ocm whoami` succeeds while the access token is valid.
- The file is owned by `sre` and has mode `600`.
- The JSON has no `refresh_token`, `offline_token`, or similar refresh field.
- After expiration, OCM fails instead of refreshing. Run this PoC again to
  replace the expired access token.
