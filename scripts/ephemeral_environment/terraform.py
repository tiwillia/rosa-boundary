"""Owned-state checks, explicit root inputs and exact saved-plan application."""

from collections import Counter
import json
import shlex

from .common import Error, digest


class Terraform:
    """Terraform operations sharing Environment's journal and injected runner."""

    def state_check(self, key):
        path = self.home / f"{key}.tfstate"
        record = self.meta["roots"].get(key)
        if path.is_symlink():
            raise Error("Terraform state may not be a symlink")
        if path.exists():
            if not record or digest(path.read_bytes()) != record.get("state_hash"):
                raise Error(f"{key}: state ownership/hash mismatch; refusing adoption")
        elif record and record.get("state_hash"):
            raise Error(f"{key}: owned state is missing")

    def record_state(self, key):
        path = self.home / f"{key}.tfstate"
        if path.exists():
            record = self.meta["roots"].setdefault(key, {})
            state = json.loads(path.read_text())
            if record.get("lineage") and state.get("lineage") != record["lineage"]:
                raise Error(f"{key}: state lineage changed unexpectedly; explicit recovery required")
            new_hash = digest(path.read_bytes())
            if new_hash != record.get("state_hash") and self.generation:
                record["generation"] = self.generation
            record["state_hash"] = new_hash
            record["lineage"] = state.get("lineage")
            record["serial"] = state.get("serial")
        self.save()

    def state_resource(self, key, resource_type, name):
        """Read one current resource from hash-verified, privately owned state."""
        self.state_check(key)
        path = self.home / f"{key}.tfstate"
        if not path.is_file():
            raise Error(f"{key}: owned resource state is missing")
        state = json.loads(path.read_text())
        matches = [
            instance["attributes"]
            for resource in state.get("resources", [])
            if (resource.get("mode") == "managed" and not resource.get("module")
                and resource.get("type") == resource_type and resource.get("name") == name)
            for instance in resource.get("instances", [])
            if not instance.get("deposed")
        ]
        if len(matches) != 1:
            raise Error(f"{key}: expected one owned {resource_type}.{name}")
        return matches[0]

    def tf(self, key, *args):
        progress = args[0] in {"init", "validate", "plan", "apply", "destroy"}
        # Existing Make targets own Boundary init/validate/plan/apply recipes.
        if key.startswith("boundary-") and args[0] in {"init", "validate", "plan", "apply"}:
            target, *extra = args
            if target == "apply" and key == "boundary-regional":
                # Never rebuild a ZIP between review and saved-plan application.
                command = ["make", "-o", "build-lambda", "apply", "ENV_FILE=/dev/null"]
            else:
                command = ["make", target, "ENV_FILE=/dev/null"]
            return self.runner.run(command, cwd=self.root(key), extra_env={
                f"TF_CLI_ARGS_{target}": shlex.join(str(x) for x in extra),
                "KEYCLOAK_ISSUER_URL": self.config["keycloak"]["issuer_url"],
            }, log=True, terraform_progress=progress)
        return self.runner.run(["terraform", *args], cwd=self.root(key), log=True,
                               terraform_progress=progress)

    def outputs(self, key):
        self.check_work()
        data = json.loads(self.tf(key, "output", "-json"))
        return {name: entry["value"] for name, entry in data.items() if not entry.get("sensitive")}

    def variables(self, key, network=None):
        c = self.config
        tags = {"EphemeralEnvironment": self.name, "EphemeralOwner": self.meta["owner"]}
        common = {"aws_region": c["region"], "default_tags": tags}
        project = f"eph-{self.name}"
        kc = {"keycloak_issuer_url": c["keycloak"]["issuer_url"],
              "keycloak_thumbprint": c["keycloak"].get("thumbprint"),
              "oidc_client_id": c["keycloak"]["client_id"],
              "enable_oidc_group_enforcement": True,
              "required_oidc_role": c["keycloak"]["required_oidc_role"]}
        if key.startswith("boundary-"):
            common.update(aws_account_id=c["account_id"], project=project, stage="dev")
        else:
            common.update(app_name=f"{project}-ta", environment="dev",
                          s3_bucket_name_suffix=f"{project}-ta",
                          internal_fqdn=c.get("ta_tls", {}).get("internal_fqdn", ""))
        if key == "boundary-account":
            common.update({k: v for k, v in c["boundary_account"].items()
                           if k != "reference_existing_github_oidc"})
            common.update(kc, legacy_policy_region=c["region"],
                          legacy_role_tag_region=c["region"],
                          bedrock_budget_tag_region=c["region"],
                          adopt_existing_resources=False,
                          create_github_actions_oidc_provider=not c["boundary_account"].get(
                              "reference_existing_github_oidc", False),
                          enable_lambda_ecr_source=False, lambda_ecr_replication_regions=[],
                          bedrock_model_agreements={}, enable_s3_replication_role=False,
                          bedrock_logging_source_regions=[c["region"]], replication=None)
        elif key == "boundary-network":
            common.update(availability_zones=[c["region"] + "a", c["region"] + "b"])
            common.update(c["network"])
        elif key == "boundary-regional":
            common.update(kc, legacy_policy_region=c["region"],
                          account_role_name_prefix=f"{project}-dev",
                          vpc_id=network["vpc_id"], subnet_ids=network["private_subnet_ids"],
                          required_groups=c["keycloak"]["required_groups"],
                          container_image=self.meta["images"]["boundary"]["uri"],
                          lambda_package_type="Zip", content_bucket_force_destroy=True,
                          ephemeral_environment=True,
                          log_retention_days=1,
                          enable_kube_proxy=False,
                          abac_tag_key=c["keycloak"]["abac_tag_key"],
                          bedrock_model_agreements={},
                          replication=None,
                           rta_server_url=self.ta_endpoint(self.outputs("ta-regional")))
        elif key == "ta-global":
            # Required by the original root even though the private override
            # disables its version resource. This is deliberately NOT a secret.
            common["backplane_client_secret"] = "script-populates-secrets-manager"
        elif key == "ta-regional":
            app = json.loads(json.dumps(c["ta_app_config"]))
            app["ocm"] = {"base_url": c["ocm"]["base_url"]}
            common.update(vpc_id=network["vpc_id"],
                          public_subnet_ids=network["public_subnet_ids"],
                          private_subnet_ids=network["private_subnet_ids"],
                          container_image=self.meta["images"]["ta"]["uri"],
                          enable_worm=False, app_config=json.dumps(app),
                           alb_certificate_arn=c.get("ta_tls", {}).get("certificate_arn", ""),
                          ephemeral_environment=True,
                          jwk_cert_url=c["ocm"]["jwk_cert_url"],
                          allowed_accounts=c["account_id"])
        return common

    def plan_apply(self, key, variables=None, destroy=False, yes=False, account=None):
        self.check_work()
        self.state_check(key)
        self.guard_account(account, yes)
        if key == "boundary-regional":
            self.check_bedrock_logging()
        if variables is not None:
            if set(variables) - self.declared_variables(key):
                raise Error(f"{key}: undeclared Terraform inputs; unsupported snapshot")
            self.generated_input(self.root(key) / "ephemeral.auto.tfvars.json", variables)
        try:
            self.tf(key, "init", "-reconfigure", "-input=false")
        finally:
            # Pin partial output bytes for retry. Init must still complete
            # successfully before any validation, planning or provider execution.
            self.record_terraform_outputs(key)
        self.check_work()
        self.tf(key, "validate")
        plan = self.home / f"{key}.plan"
        args = ["plan", "-input=false", "-out=" + str(plan)]
        if destroy:
            args.append("-destroy")
        self.check_work()
        try:
            self.tf(key, *args)
        finally:
            self.record_terraform_outputs(key)
        rendered = self.tf(key, "show", "-json", str(plan))
        changes = json.loads(rendered).get("resource_changes", [])
        state_path = self.home / f"{key}.tfstate"
        prior_state = json.loads(state_path.read_text()) if state_path.exists() else {}
        has_managed_state = any(resource.get("mode") == "managed"
                                for resource in prior_state.get("resources", []))
        summary = Counter()
        for item in changes:
            if item.get("mode") == "data":
                continue
            change = item["change"]
            actions = change["actions"]
            before = change.get("before")
            if isinstance(before, dict) and ("tags_all" in before or "tags" in before):
                owner_tags = before.get("tags_all") or before.get("tags") or {}
                if owner_tags.get("EphemeralOwner") != self.meta["owner"]:
                    raise Error("Refreshed plan found a resource no longer tagged as owned by this environment")
            if item.get("type") in {"aws_secretsmanager_secret_version",
                                    "aws_s3_bucket_object_lock_configuration",
                                    "aws_ecr_replication_configuration",
                                    "aws_bedrock_foundation_model_agreement"}:
                raise Error("Plan would manage account-wide replication/model agreements, "
                            "plaintext secret state, or immutable retention in an ephemeral environment")
            if change.get("importing"):
                raise Error("Resource import/adoption is forbidden")
            if not has_managed_state and not destroy and actions not in (["create"], ["no-op"]):
                raise Error("Fresh environment plan would change an existing resource")
            if destroy and actions not in (["delete"], ["no-op"]):
                raise Error("Destroy plan contains non-deletion changes")
            action = ",".join(actions)
            summary[{"no-op": "unchanged", "delete,create": "replace",
                     "create,delete": "replace"}.get(action, action)] += 1
        if key == "boundary-regional" and not destroy and any(
            item.get("mode") != "data" and item["change"]["actions"] != ["no-op"]
            for item in changes
        ):
            self.ensure_quiescent()
        review = self.home / f"{key}.review.txt"
        review.write_bytes(self.tf(key, "show", "-no-color", str(plan)))
        review.chmod(0o600)
        plan_hash = digest(plan.read_bytes())
        self.approve(
            f"Terraform {'destroy' if destroy else 'apply'}: {key}\n"
            + ("\n".join(f"  {action.capitalize()}: {count}" for action, count in sorted(summary.items()))
               if summary else "  No resource changes")
            + f"\n  Full plan: {review}", yes,
        )
        self.guard_account(account, yes)
        if key == "boundary-regional":
            self.check_bedrock_logging()
        if digest(plan.read_bytes()) != plan_hash:
            raise Error("Saved plan changed after approval")
        self.check_work()
        self.meta["roots"].setdefault(key, {})["pending_plan"] = plan_hash
        self.meta["roots"][key].update(
            pending_generation=self.generation, touched=True,
            pending_addresses=sorted({address for item in changes if item.get("mode") != "data"
                                      for address in (item["address"], item.get("previous_address"))
                                      if address}),
        )
        self.save()
        try:
            self.tf(key, "apply", "-input=false", str(plan))
        finally:
            # Partial applies retain owned state for explicit recovery, not adoption.
            self.record_state(key)
        self.meta["roots"][key].pop("pending_plan", None)
        self.meta["roots"][key].pop("pending_generation", None)
        self.meta["roots"][key].pop("pending_addresses", None)
        self.meta["roots"][key]["destroyed"] = destroy
        if self.generation:
            self.meta["roots"][key]["generation"] = self.generation
        self.save()
