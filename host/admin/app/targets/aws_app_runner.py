"""AWS App Runner deployment target — containerized web/api services.

Deploy flow:
  1. Ensure the shared IAM access role App Runner uses to pull from ECR
     exists (`homebox-apprunner-ecr-access`, trusted by
     build.apprunner.amazonaws.com, with AWS's managed
     AWSAppRunnerServicePolicyForECRAccess attached). IAM is a Query-protocol
     service on the global endpoint — awslib signs it as us-east-1.
  2. Push the locally-built image to ECR via registry.ecr_push (pulling the
     ref first when a service pins an upstream image that was never built
     here).
  3. Create-or-update the App Runner service (x-amz-json-1.0 protocol):
     ListServices → CreateService, or UpdateService + StartDeployment when
     the image ref is unchanged (ECR `:latest` re-pull needs an explicit
     deployment).
  4. Poll DescribeService until RUNNING.
  5. Associate the custom domain (idempotently). App Runner hands back a
     DNSTarget plus ACM certificate-validation records — this provider has
     no Cloudflare access, so both are returned in TargetResult for the
     orchestrator to write: cname_target and state.extra_dns_records.
"""

from __future__ import annotations

import asyncio
import json
import re
import urllib.parse
from typing import Any

import httpx

from . import registry
from .awslib import AwsClient, AwsError
from .base import DeployTarget, TargetDeployCtx, TargetError, TargetResult

# App Runner service names: [A-Za-z0-9-_], at most 40 chars.
_NAME_MAX = 40

_ECR_ACCESS_ROLE = "homebox-apprunner-ecr-access"
_ECR_ACCESS_POLICY_ARN = (
    "arn:aws:iam::aws:policy/service-role/AWSAppRunnerServicePolicyForECRAccess"
)
_ASSUME_ROLE_POLICY = json.dumps({
    "Version": "2012-10-17",
    "Statement": [{
        "Effect": "Allow",
        "Principal": {"Service": "build.apprunner.amazonaws.com"},
        "Action": "sts:AssumeRole",
    }],
})

# Deployment polling knobs — module constants so tests can zero the interval.
_POLL_INTERVAL = 5.0
_POLL_TIMEOUT = 300.0

_FAILED_STATUSES = ("CREATE_FAILED", "DELETE_FAILED")


def _service_name(ctx: TargetDeployCtx) -> str:
    """Sanitize ctx.resource_name into a valid App Runner service name."""
    name = re.sub(r"[^A-Za-z0-9_-]+", "-", ctx.resource_name)
    name = re.sub(r"-{2,}", "-", name)
    return name[:_NAME_MAX].strip("-_")


class AppRunnerTarget(DeployTarget):
    """AWS App Runner (web/api container services)."""

    provider = "aws"
    variant = "app_runner"

    def __init__(
        self,
        creds: dict[str, Any],
        config: dict[str, Any] | None = None,
        state: dict[str, Any] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        creds = creds or {}
        self._config = config or {}
        self._state = state or {}
        self._account_id: str = str(creds.get("account_id") or "")
        self._aws = AwsClient(
            creds.get("key_id") or "",
            creds.get("secret") or "",
            creds.get("region") or "us-east-1",
            transport=transport,  # injectable for tests (httpx.MockTransport)
        )

    # ───── plumbing ───────────────────────────────────────────────────────────

    async def _apprunner(self, op: str, payload: dict) -> dict:
        """One App Runner call — the service speaks x-amz-json-1.0."""
        return await self._aws.json_call(
            "apprunner", f"AppRunner.{op}", payload, json_version="1.0"
        )

    async def _iam(self, action: str, params: dict[str, str]) -> None:
        """One IAM Query-protocol call (Version 2010-05-08, global endpoint).
        Parameters — including JSON policy documents — are form-URL-encoded."""
        form = {"Action": action, "Version": "2010-05-08", **params}
        await self._aws.request(
            "iam",
            body=urllib.parse.urlencode(sorted(form.items())),
            headers={"content-type": "application/x-www-form-urlencoded"},
        )

    async def _describe(self, service_arn: str) -> dict:
        out = await self._apprunner("DescribeService", {"ServiceArn": service_arn})
        return out.get("Service") or {}

    # ───── deploy steps ───────────────────────────────────────────────────────

    async def _ensure_ecr_access_role(self, ctx: TargetDeployCtx) -> str:
        """Ensure the shared ECR-pull role exists and return its ARN.
        Idempotent: EntityAlreadyExists is fine, AttachRolePolicy re-attaches
        a managed policy without error."""
        try:
            await self._iam("CreateRole", {
                "RoleName": _ECR_ACCESS_ROLE,
                "AssumeRolePolicyDocument": _ASSUME_ROLE_POLICY,
            })
            await ctx.emit(f"created IAM role {_ECR_ACCESS_ROLE}")
        except AwsError as e:
            if "EntityAlreadyExists" not in (e.code or ""):
                raise TargetError(
                    f"IAM role {_ECR_ACCESS_ROLE} create failed: {e}"
                ) from e
        try:
            await self._iam("AttachRolePolicy", {
                "RoleName": _ECR_ACCESS_ROLE,
                "PolicyArn": _ECR_ACCESS_POLICY_ARN,
            })
        except AwsError as e:
            raise TargetError(
                f"attaching the App Runner ECR access policy failed: {e}"
            ) from e

        account_id = self._account_id
        if not account_id:
            try:
                ident = await self._aws.sts_get_caller_identity()
            except AwsError as e:
                raise TargetError(
                    f"could not resolve the AWS account id via STS: {e}"
                ) from e
            account_id = self._account_id = str(ident.get("account") or "")
        if not account_id:
            raise TargetError(
                "could not determine the AWS account id for the ECR access role."
            )
        return f"arn:aws:iam::{account_id}:role/{_ECR_ACCESS_ROLE}"

    async def _ensure_local_image(self, ctx: TargetDeployCtx) -> None:
        """ctx.image is normally a locally-built tag, but a service may pin an
        upstream ref (e.g. postgres:16) that was never built here — pull it
        first so docker tag/push has something to work with."""
        code, _ = await registry._run(
            ["docker", "image", "inspect", ctx.image], timeout=60)
        if code == 0:
            return
        await ctx.emit(f"pulling {ctx.image}…")
        code, out = await registry._run(["docker", "pull", ctx.image], timeout=900)
        if code:
            raise TargetError(f"docker pull {ctx.image} failed: {out[-500:]}")

    async def _find_service(self, name: str) -> dict | None:
        """Locate an existing App Runner service by name (paginated)."""
        token: str | None = None
        while True:
            payload: dict[str, Any] = {}
            if token:
                payload["NextToken"] = token
            out = await self._apprunner("ListServices", payload)
            for svc in out.get("ServiceSummaryList") or []:
                if svc.get("ServiceName") == name:
                    return svc
            token = out.get("NextToken")
            if not token:
                return None

    async def _wait_running(self, service_arn: str, ctx: TargetDeployCtx) -> dict:
        """Poll DescribeService until RUNNING (or a terminal failure)."""
        waited = 0.0
        last_status = ""
        while True:
            svc = await self._describe(service_arn)
            status = svc.get("Status") or ""
            if status == "RUNNING":
                return svc
            if status in _FAILED_STATUSES:
                raise TargetError(
                    f"App Runner service ended in status {status} — check the "
                    "service's event log in the AWS console."
                )
            if status != last_status:
                await ctx.emit(f"service status: {status or '?'}…")
                last_status = status
            if waited >= _POLL_TIMEOUT:
                raise TargetError(
                    f"timed out after {int(_POLL_TIMEOUT)}s waiting for the App "
                    f"Runner service to reach RUNNING (last status: {status})."
                )
            await asyncio.sleep(_POLL_INTERVAL)
            # A zeroed interval (tests) still counts toward the timeout so the
            # loop stays bounded.
            waited += _POLL_INTERVAL if _POLL_INTERVAL > 0 else 5.0

    async def _ensure_custom_domain(
        self, service_arn: str, ctx: TargetDeployCtx
    ) -> tuple[str | None, list[dict[str, str]]]:
        """Associate ctx.hostname with the service (idempotently). Returns
        (DNSTarget, certificate-validation records) for the orchestrator —
        this provider cannot write DNS itself."""
        hostname = ctx.hostname or ""
        out = await self._apprunner(
            "DescribeCustomDomains", {"ServiceArn": service_arn})
        dns_target = out.get("DNSTarget")
        domain = next(
            (d for d in out.get("CustomDomains") or []
             if (d.get("DomainName") or "").lower() == hostname.lower()),
            None,
        )
        if domain is None:
            await ctx.emit(f"associating custom domain {hostname}…")
            try:
                assoc = await self._apprunner("AssociateCustomDomain", {
                    "ServiceArn": service_arn,
                    "DomainName": hostname,
                    "EnableWWWSubdomain": False,
                })
                dns_target = assoc.get("DNSTarget") or dns_target
                domain = assoc.get("CustomDomain") or {}
            except AwsError as e:
                # Racing another deploy: the domain landed between our
                # describe and associate. Anything else propagates.
                if "already associated" not in str(e).lower():
                    raise
                domain = {}
        records = [
            {"name": r.get("Name") or "", "type": r.get("Type") or "",
             "value": r.get("Value") or ""}
            for r in (domain or {}).get("CertificateValidationRecords") or []
        ]
        return dns_target, records

    # ───── contract ───────────────────────────────────────────────────────────

    async def validate(self) -> None:
        try:
            await self._aws.sts_get_caller_identity()
        except AwsError as e:
            raise TargetError(
                f"AWS credential check failed: {e} — verify the access key id "
                "and secret in Integrations."
            ) from e

    async def deploy(self, ctx: TargetDeployCtx) -> TargetResult:
        if not ctx.image:
            raise TargetError(
                "App Runner needs a container image, but this service produced "
                "none — check the service's build output."
            )
        name = _service_name(ctx)
        try:
            role_arn = await self._ensure_ecr_access_role(ctx)

            await self._ensure_local_image(ctx)
            await ctx.emit(f"pushing {ctx.image} to ECR…")
            # ECR repository names are lowercase-only.
            remote_ref = await registry.ecr_push(
                self._aws, image_name=name.lower(), local_tag=ctx.image)

            source_config = {
                "ImageRepository": {
                    "ImageIdentifier": remote_ref,
                    "ImageRepositoryType": "ECR",
                    "ImageConfiguration": {
                        "Port": str(ctx.internal_port or 8080),
                        "RuntimeEnvironmentVariables": ctx.env_vars,
                    },
                },
                "AuthenticationConfiguration": {"AccessRoleArn": role_arn},
                "AutoDeploymentsEnabled": False,
            }

            existing = await self._find_service(name)
            if existing is None:
                await ctx.emit(f"creating App Runner service {name}…")
                out = await self._apprunner("CreateService", {
                    "ServiceName": name,
                    "SourceConfiguration": source_config,
                    "InstanceConfiguration": {"Cpu": "1024", "Memory": "2048"},
                })
                service_arn = (out.get("Service") or {}).get("ServiceArn") or ""
                if not service_arn:
                    raise TargetError(
                        "App Runner CreateService returned no ServiceArn.")
            else:
                service_arn = existing.get("ServiceArn") or ""
                current = await self._describe(service_arn)
                current_ref = (
                    ((current.get("SourceConfiguration") or {})
                     .get("ImageRepository") or {}).get("ImageIdentifier")
                )
                await ctx.emit(f"updating App Runner service {name}…")
                await self._apprunner("UpdateService", {
                    "ServiceArn": service_arn,
                    "SourceConfiguration": source_config,
                })
                if current_ref == remote_ref:
                    # Same tag (ECR :latest) → UpdateService alone won't
                    # re-pull; force a fresh deployment.
                    await ctx.emit("image ref unchanged — starting deployment…")
                    await self._apprunner(
                        "StartDeployment", {"ServiceArn": service_arn})

            svc = await self._wait_running(service_arn, ctx)
            endpoint = (svc.get("ServiceUrl") or "").removeprefix("https://")
            await ctx.emit(f"service running at {endpoint}")

            state: dict[str, Any] = {
                "service_arn": service_arn,
                "service_name": name,
                "url": endpoint,
            }
            cname_target: str | None = None
            if ctx.hostname:
                dns_target, records = await self._ensure_custom_domain(
                    service_arn, ctx)
                cname_target = dns_target or endpoint
                state["extra_dns_records"] = records
                if records:
                    await ctx.emit(
                        f"custom domain needs {len(records)} certificate "
                        "validation record(s) — handing them to DNS."
                    )
        except AwsError as e:
            raise TargetError(f"App Runner deploy failed: {e}") from e

        # App Runner terminates TLS with its own cert; the CNAME must stay
        # DNS-only (grey cloud) so ACM validation and cert pinning work.
        return TargetResult(
            endpoint=endpoint,
            cname_target=cname_target,
            proxied=False,
            state=state,
        )

    async def destroy(self, state: dict[str, Any]) -> None:
        service_arn = (state or {}).get("service_arn")
        if not service_arn:
            return
        try:
            await self._apprunner("DeleteService", {"ServiceArn": service_arn})
        except AwsError as e:
            code = e.code or ""
            # Already gone, or a delete is already in flight — both fine.
            if "ResourceNotFoundException" in code or "InvalidStateException" in code:
                return
            raise TargetError(f"App Runner destroy failed: {e}") from e

    async def probe(self, state: dict[str, Any]) -> bool:
        service_arn = (state or {}).get("service_arn")
        if not service_arn:
            return False
        try:
            svc = await self._describe(service_arn)
        except AwsError:
            return False
        return svc.get("Status") == "RUNNING"
