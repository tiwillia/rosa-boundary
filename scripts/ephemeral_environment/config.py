"""Normalize nonsecret defaults and validate explicit deployment boundaries."""

import json
import math
import os
from pathlib import Path
import re
from urllib.parse import urlparse

from .common import Error, INTEGRATION_OCM


# OCM environments share production SSO, independently of their API base URL.
DEFAULT_OCM_JWKS_URL = "https://sso.redhat.com/auth/realms/redhat-external/protocol/openid-connect/certs"


def object_keys(value, required, optional=(), label="config"):
    if not isinstance(value, dict):
        raise Error(f"{label} must be an object")
    if set(value) - set(required) - set(optional) or set(required) - set(value):
        raise Error(f"{label} has missing or unsupported fields")


def https_url(value, label):
    if not isinstance(value, str) or value != value.strip():
        raise Error(f"{label} must be an HTTPS URL")
    try:
        parsed = urlparse(value)
    except ValueError:
        raise Error(f"{label} must be an HTTPS URL") from None
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise Error(f"{label} must be an HTTPS URL without credentials/query")


def string_list(value, label):
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item.strip() for item in value
    ):
        raise Error(f"{label} must be a list of nonempty strings")


def reject_credentials(value):
    """Reject credential fields and URL credentials before serializing app config."""
    if isinstance(value, dict):
        for key, child in value.items():
            if re.search(r"(password|client.?(?:id|secret)|private.?key|access.?key|token)", key, re.I):
                raise Error("Plaintext credentials are forbidden in configuration")
            reject_credentials(child)
    elif isinstance(value, list):
        for child in value:
            reject_credentials(child)
    elif isinstance(value, str):
        try:
            parsed = urlparse(value.strip())
            # Proxy credentials are values, not credential-named fields.
            if parsed.netloc or parsed.scheme.lower() in {"http", "https", "socks5", "socks5h"}:
                if (not parsed.netloc or parsed.username is not None
                        or parsed.password is not None or parsed.query or parsed.fragment):
                    raise Error("Credential-bearing or malformed URLs are forbidden in configuration")
        except ValueError:
            raise Error("Malformed URL in application configuration") from None


def load_config(path, repository):
    if Path(path).expanduser().resolve().is_relative_to(repository.resolve()):
        raise Error("Config JSON must live outside the source repository")
    try:
        text = Path(path).read_text()
    except OSError as error:
        raise Error(f"Cannot read config JSON ({type(error).__name__})") from None
    except UnicodeError:
        raise Error("Config JSON must be UTF-8 text") from None
    try:
        value = json.loads(text)
    except json.JSONDecodeError as error:
        raise Error(f"Invalid config JSON at line {error.lineno}, column {error.colno}: "
                    f"{error.msg}") from None
    object_keys(
        value,
        ("account_id", "region", "keycloak", "vault", "boundary_account"),
        ("profile", "state_dir", "ocm", "network", "ta_tls", "ta_app_config"),
    )
    value.setdefault("profile", "default")
    if "state_dir" not in value:
        xdg_state = os.environ.get("XDG_STATE_HOME")
        if xdg_state and not Path(xdg_state).is_absolute():
            raise Error("XDG_STATE_HOME must be absolute")
        state_home = Path(xdg_state) if xdg_state else Path.home() / ".local/state"
        value["state_dir"] = str(state_home / "rosa-ephemeral")
    if not isinstance(value["account_id"], str) or not re.fullmatch(r"\d{12}", value["account_id"]):
        raise Error("account_id must be a 12-digit string")
    if not isinstance(value["profile"], str) or not re.fullmatch(r"[\w.@+-]+", value["profile"]):
        raise Error("AWS profile must be a nonempty profile name")
    if not isinstance(value["region"], str) or not re.fullmatch(r"[a-z]{2}(?:-[a-z]+)+-\d+", value["region"]):
        raise Error("An explicit AWS region is required")
    if not isinstance(value["state_dir"], str) or not value["state_dir"].strip():
        raise Error("state_dir must be an absolute path outside the source repository")
    state = Path(value["state_dir"]).expanduser()
    if not state.is_absolute() or state.resolve().is_relative_to(repository.resolve()):
        raise Error("state_dir must be absolute and outside the source repository")
    value["state_dir"] = str(state.resolve())
    kc = value["keycloak"]
    object_keys(kc, ("issuer_url",),
                ("client_id", "abac_tag_key", "required_groups", "required_oidc_role",
                 "thumbprint", "existing_provider_arn"), label="keycloak")
    kc.setdefault("client_id", "rosa-boundary-sre")
    kc.setdefault("abac_tag_key", "username")
    kc.setdefault("required_groups", ["ai-sd-sre", "eng-openshift-enterprise", "sre-team"])
    kc.setdefault("required_oidc_role", "ai-sd-sre")
    https_url(kc["issuer_url"], "keycloak issuer_url")
    if not re.search(r"/realms/[^/]+/?$", kc["issuer_url"]):
        raise Error("Keycloak issuer_url must end in /realms/REALM")
    if "thumbprint" in kc and (
        not isinstance(kc["thumbprint"], str)
        or not re.fullmatch(r"[a-fA-F0-9]{40}", kc["thumbprint"])
    ):
        raise Error("Keycloak thumbprint must be SHA1 hex")
    if (not isinstance(kc["client_id"], str) or not kc["client_id"].strip()
            or not isinstance(kc["required_groups"], list) or not kc["required_groups"]):
        raise Error("Keycloak client_id and required_groups must be explicit")
    if not all(isinstance(x, str) and x.strip() for x in kc["required_groups"]):
        raise Error("required_groups must contain nonempty strings")
    if not isinstance(kc["required_oidc_role"], str) or not kc["required_oidc_role"].strip():
        raise Error("Keycloak required_oidc_role must name the session role tag")
    if kc["abac_tag_key"] not in ("uuid", "username"):
        raise Error("Keycloak abac_tag_key must be uuid or username")
    if kc.get("existing_provider_arn"):
        expected = (f"arn:aws:iam::{value['account_id']}:oidc-provider/"
                    + kc["issuer_url"].removeprefix("https://").rstrip("/"))
        if kc["existing_provider_arn"] != expected:
            raise Error("existing_provider_arn must match the configured account and Keycloak issuer")
    vault = value["vault"]
    object_keys(vault, ("path",), ("address",), label="vault")
    vault.setdefault("address", os.environ.get("VAULT_ADDR"))
    https_url(vault["address"], "Vault address")
    if not all(isinstance(x, str) and x.strip() and not x.startswith("-") for x in vault.values()):
        raise Error("Vault references must be nonempty strings")
    ocm = value.setdefault("ocm", {})
    object_keys(ocm, (), ("base_url", "jwk_cert_url"), label="ocm")
    ocm.setdefault("base_url", INTEGRATION_OCM)
    if not isinstance(ocm["base_url"], str) or ocm["base_url"].rstrip("/") != INTEGRATION_OCM:
        raise Error("Only OCM integration is supported")
    value["ocm"].setdefault("jwk_cert_url", DEFAULT_OCM_JWKS_URL)
    https_url(value["ocm"]["jwk_cert_url"], "OCM jwk_cert_url")
    object_keys(value["boundary_account"],
                ("bedrock_budget_notification_email",),
                ("bedrock_monthly_budget_usd", "reference_existing_github_oidc"),
                label="boundary_account")
    account = value["boundary_account"]
    account.setdefault("bedrock_monthly_budget_usd", 25)
    budget = account["bedrock_monthly_budget_usd"]
    if (isinstance(budget, bool) or not isinstance(budget, (int, float))
            or (isinstance(budget, float) and not math.isfinite(budget)) or budget <= 0):
        raise Error("A positive Bedrock budget must be configured")
    if (not isinstance(account["bedrock_budget_notification_email"], str)
            or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", account["bedrock_budget_notification_email"])):
        raise Error("A Bedrock budget notification email is required")
    if not isinstance(account.get("reference_existing_github_oidc", False), bool):
        raise Error("reference_existing_github_oidc must be boolean")
    app = value.setdefault("ta_app_config", {})
    if not isinstance(app, dict):
        raise Error("ta_app_config must be an object")
    app.setdefault("roles", [{"id": "SREP", "amsResource": "BackplaneOsdSrepResource"}])
    if not isinstance(app["roles"], list) or not app["roles"]:
        raise Error("TA roles must be a nonempty list")
    reject_credentials(app)
    if set(app) - {"roles", "actions", "workers", "backplane", "aws"}:
        raise Error("Unsupported ta_app_config fields (OCM is configured separately)")
    if "aws" in app and app["aws"].get("endpoint_url"):
        raise Error("Custom AWS endpoints are forbidden")
    backplane = app.get("backplane", {})
    if not isinstance(backplane, dict):
        raise Error("TA backplane must be an object")
    if "base_url" in backplane:
        raise Error("TA backplane base_url must come from Vault, not configuration")
    actions = app.setdefault("actions", {})
    object_keys(actions, (),
                ("allowed_namespaces", "aws_allowed_regions", "aws_allowed_operations", "allowed_secrets"),
                label="TA actions")
    # Disposable dev matches stage: an empty list selects permissive Kubernetes
    # authorization, including bypassing the separate secret allowlist.
    actions.setdefault("allowed_namespaces", [])
    string_list(actions["allowed_namespaces"], "TA allowed_namespaces")
    actions.setdefault("aws_allowed_regions", [value["region"]])
    if actions["aws_allowed_regions"] != [value["region"]]:
        raise Error("TA aws_allowed_regions must contain only the configured region")
    # Runtime already defaults these to empty. Keep absent keys absent so older
    # environments retain their canonical configuration/ownership hashes.
    for key in ("aws_allowed_operations", "allowed_secrets"):
        if key in actions:
            string_list(actions[key], f"TA {key}")
    network = value.setdefault("network", {})
    object_keys(network, (), ("vpc_cidr", "availability_zones", "public_subnet_cidrs",
                             "private_subnet_cidrs", "nat_strategy"), label="network")
    if "ta_tls" in value:
        tls = value["ta_tls"]
        object_keys(tls, ("internal_fqdn", "certificate_arn"), label="ta_tls")
        if (not isinstance(tls["internal_fqdn"], str)
                or not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", tls["internal_fqdn"])):
            raise Error("TA TLS internal_fqdn must be a DNS hostname")
        expected = f"arn:aws:acm:{value['region']}:{value['account_id']}:certificate/"
        if not isinstance(tls["certificate_arn"], str) or not tls["certificate_arn"].startswith(expected):
            raise Error("TA certificate must belong to the configured account and region")
    return value
