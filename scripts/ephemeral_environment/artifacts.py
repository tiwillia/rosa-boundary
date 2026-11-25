"""Build and publish immutable same-snapshot images and Lambda dependencies."""

import os
import re
import uuid

from .common import Error, canonical, digest
from .runtime import CommandError
from .snapshot import tree_manifest


class Artifacts:
    def publish_images(self):
        self.check_work()
        if self.meta.get("pending_ecr") or self.meta.get("pending_publications"):
            raise Error("Interrupted ECR operation requires the explicit recover command")
        for component in ("boundary", "ta"):
            if component in self.meta["images"]:
                continue
            repository = f"eph-{self.name}/{component}"
            if component in self.meta.get("repositories", {}):
                response = self.aws("ecr", "describe-repositories", "--repository-names", repository)
                description = response["repositories"][0]
                tags = self.aws("ecr", "list-tags-for-resource", "--resource-arn",
                                description["repositoryArn"])
                if not any(t["Key"] == "EphemeralOwner" and t["Value"] == self.meta["owner"]
                           for t in tags.get("tags", [])):
                    raise Error("Refusing to reuse an unowned ECR repository")
            else:
                self.meta.setdefault("pending_ecr", {})[component] = {
                    "repository": repository, "generation": self.generation,
                }
                self.save()
                try:
                    response = self.aws("ecr", "create-repository", "--repository-name", repository,
                                        "--image-tag-mutability", "IMMUTABLE", "--tags",
                                        f"Key=EphemeralOwner,Value={self.meta['owner']}",
                                        f"Key=EphemeralEnvironment,Value={self.name}")
                except CommandError as error:
                    if b"RepositoryAlreadyExistsException" in error.stderr:
                        self.meta["pending_ecr"].pop(component)
                        self.save()
                    raise
                description = response["repository"]
            uri = description["repositoryUri"]
            expected = f"{self.config['account_id']}.dkr.ecr.{self.config['region']}.amazonaws.com/"
            if not uri.startswith(expected) or uri != expected + repository:
                raise Error("Unexpected ECR publication destination")
            self.meta.setdefault("repositories", {})[component] = repository
            self.meta.get("pending_ecr", {}).pop(component, None)
            self.save()
            registry = uri.split("/")[0]
            password = self.aws("ecr", "get-login-password", json_output=False).strip()
            authfile = self.home / "registry-auth.json"
            try:
                self.runner.run(["podman", "login", "--authfile", str(authfile),
                                 "--username", "AWS", "--password-stdin", registry],
                                input=password, build=True)
                tag = self.meta["source"]["fingerprint"][:24] + "-" + uuid.uuid4().hex[:12]
                image = f"{uri}:{tag}"
                self.check_work()
                if component == "boundary":
                    github = {key: os.environ[key] for key in (
                        "GITHUB_TOKEN", "GITHUB_APP_ID", "GITHUB_APP_PEM", "GITHUB_APP_INSTALL_ID"
                    ) if os.environ.get(key)}
                    source = self.meta["source"]
                    revision = source["commit"]
                    if source["selection"] == "working-tree":
                        # Match the image/RTA version contract. Exact snapshot
                        # provenance is separately pinned by fingerprint/digest.
                        revision += "-dirty"
                    self.runner.run(["make", "build-amd64", f"IMAGE_NAME={uri}",
                                     f"TAG={tag}", f"RTA_COMMIT={revision}"],
                                    cwd=self.work, build=True, log=True, extra_env=github)
                else:
                    self.runner.run(["make", "build-amd64", f"IMAGE_NAME={uri}", f"TAG={tag}"],
                                    cwd=self.work / "components/trusted-actions", build=True, log=True)
                local_image = image + "-amd64"
                self.check_work()
                inspection = self.runner.json(["podman", "image", "inspect", local_image],
                                              label="podman image inspect", build=True)
                if inspection[0].get("Architecture") != "amd64":
                    raise Error("Built image architecture is not amd64")
                digest_file = self.source.parent / f"{component}.{tag}.push-digest"
                self.meta.setdefault("pending_publications", {})[component] = {
                    "repository": repository, "tag": tag, "generation": self.generation,
                    "local_id": inspection[0]["Id"], "source": self.meta["source"]["fingerprint"],
                    "digest_file": str(digest_file), "uri": uri,
                }
                self.save()
                self.runner.run(["podman", "push", "--authfile", str(authfile),
                                 "--digestfile", str(digest_file),
                                 local_image, f"docker://{image}"], build=True)
                details = self.aws("ecr", "describe-images", "--repository-name", repository,
                                   "--image-ids", f"imageTag={tag}")
                sha = details["imageDetails"][0]["imageDigest"]
                if not re.fullmatch(r"sha256:[a-f0-9]{64}", sha):
                    raise Error("ECR returned an invalid image digest")
                if digest_file.read_text().strip() != sha:
                    raise Error("Published ECR digest differs from podman's pushed manifest")
                self.meta["images"][component] = {
                    "uri": f"{uri}@{sha}", "local_id": inspection[0]["Id"],
                    "architecture": "amd64", "source": self.meta["source"]["fingerprint"],
                }
                self.meta["pending_publications"].pop(component)
                self.save()
            finally:
                authfile.unlink(missing_ok=True)

    def build_lambda(self):
        """Reuse verified same-generation dependencies; never rebuild after plan."""
        self.check_work()
        package = self.work / "lambda/create-investigation"
        generation = self.meta["generations"][self.generation]
        existing = (digest(canonical(tree_manifest(package))) if package.is_dir() else None)
        if (existing and existing == generation.get("lambda_package_fingerprint")
                and not generation.get("lambda_build_fingerprint")):
            return
        try:
            self.runner.run(["make", "build-lambda", "ENV_FILE=/dev/null"],
                            cwd=self.root("boundary-regional"), build=True, log=True,
                            extra_env={"KEYCLOAK_ISSUER_URL": self.config["keycloak"]["issuer_url"],
                                       "STAGE_KEYCLOAK_ISSUER_URL": "",
                                       "STAGE_OIDC_CLIENT_ID": "",
                                       "STAGE_KEYCLOAK_THUMBPRINT": ""})
        finally:
            # A failed installer can leave dependencies behind. Pin those bytes
            # for a safe retry, without claiming a complete reusable package.
            if package.is_dir():
                generation["lambda_build_fingerprint"] = digest(canonical(tree_manifest(package)))
                self.save()
        if not package.is_dir():
            raise Error("Lambda build did not produce its isolated package directory")
        generation["lambda_package_fingerprint"] = generation.pop("lambda_build_fingerprint")
        self.save()
        self.check_work()
