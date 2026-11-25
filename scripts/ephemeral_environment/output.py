"""Concise, allowlisted command summaries; detailed payloads remain opt-in."""

import json

from .common import ROOTS


def environment_payload(name, config, home, meta):
    """Preserve the CLI's detailed JSON schema, including recorded provenance."""
    return {
        "name": name, "status": meta["status"],
        "account_id": config["account_id"], "region": config["region"],
        "source": meta["source"], "state_dir": str(home),
        "images": meta["images"], "outputs": meta.get("outputs", {}),
        "connection": meta.get("connection", {}),
        "verification": meta.get("verification", {}),
        "roots": meta.get("roots", {}),
        "active_generation": meta.get("active_generation"),
        "generation_count": len(meta.get("generations", {})),
        "source_request": meta.get("source_requests", [{}])[-1],
        "outputs_generation": meta.get("outputs_generation"),
        "status_is_recorded_not_live": True,
    }


def root_status(record):
    if record.get("pending_plan"):
        return "interrupted"
    if record.get("destroyed"):
        return "destroyed"
    if record.get("touched"):
        return "applied"
    return "not applied"


def recorded_status(payload):
    status = payload["status"]
    pending = any(record.get("pending_plan") for record in payload["roots"].values())
    if pending:
        if status in {"destroying", "deploying"}:
            return "destroy interrupted" if status == "destroying" else "deploy interrupted"
        return "interrupted operation"
    # A process may have stopped before it reached the first journaled apply.
    if status in {"destroying", "deploying"}:
        return "destroy incomplete" if status == "destroying" else "deploy incomplete"
    return status


def summary(action, payload):
    lines = [f"Ephemeral environment: {payload['name']} ({action})"]
    if action == "reset":
        lines.append(f"Local record: {payload['status']}")
        if payload.get("archive"):
            lines.append(f"Archived local path: {payload['archive']}")
        lines.extend((
            "AWS: no changes; no cloud checks",
            f"Next: {payload.get('next_step', 'make ephemeral-env')}",
        ))
        return "\n".join(lines)

    lines.extend((
        f"Target: {payload['account_id']} / {payload['region']}",
        f"Recorded status (not live): {recorded_status(payload)}",
    ))
    if action == "status":
        lines.append("Cloud checks: none; cached local record only")
    source = payload["source"]
    selection = "working tree" if source.get("selection") == "working-tree" else "commit"
    fingerprint = source.get("fingerprint", "")[:12]
    commit = source.get("commit", "")[:12]
    if commit or fingerprint:
        lines.append(f"Source: {selection} {commit}; fingerprint {fingerprint}")
    lines.append(f"State directory: {payload['state_dir']}")
    roots = payload["roots"]
    if roots:
        lines.extend(("", "Roots (recorded)"))
        lines.extend(f"  {key}: {root_status(roots[key])}" for key in ROOTS if key in roots)
    if any(record.get("pending_plan") for record in roots.values()):
        lines.extend(("", "Next: review failure diagnostics; retry or use recover for interrupted work."))
    if action == "deploy" and recorded_status(payload) == "deployed":
        connection = payload["connection"]
        if connection.get("cli_command"):
            lines.append(f"CLI: {connection['cli_command']}")
        if connection.get("trusted_actions_url"):
            lines.append(f"Trusted Actions (private, VPC only): {connection['trusted_actions_url']}")
    return "\n".join(lines)


def print_result(action, payload, *, json_output=False):
    print(json.dumps(payload, indent=2) if json_output else summary(action, payload))
