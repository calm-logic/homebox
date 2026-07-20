"""Container-registry plumbing for serverless container targets: ensure a
repository exists on the provider, authenticate the local docker daemon, and
tag+push the locally-built image. Both flows return the fully-qualified remote
image reference the provider's runtime pulls.

Registry choice is provider-native (ECR for App Runner, Artifact Registry for
Cloud Run, Cloudflare's managed registry for Containers) — the runtimes pull
in-provider images with the least IAM ceremony and no egress fees.
"""

from __future__ import annotations

import base64

from .base import TargetError

# One shared repo per provider account; images are separated by name:tag —
# fewer resources to create/authorize than repo-per-service (ECR is the
# exception: its repository IS the image name, so we create per-image repos).
GCP_REPO_ID = "homebox"


async def _run(cmd: list[str], *, timeout: int = 900,
               input_text: str | None = None) -> tuple[int, str]:
    """Local wrapper over deploy._run with optional stdin (docker login)."""
    import asyncio
    if input_text is None:
        from ..deploy import _run as deploy_run
        return await deploy_run(cmd, timeout=timeout)
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    try:
        out, _ = await asyncio.wait_for(
            proc.communicate(input_text.encode()), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return 124, "command timed out"
    return proc.returncode or 0, out.decode("utf-8", "replace")


async def _docker_login(registry: str, username: str, password: str) -> None:
    code, out = await _run(
        ["docker", "login", registry, "-u", username, "--password-stdin"],
        input_text=password, timeout=60,
    )
    if code:
        raise TargetError(f"docker login to {registry} failed: {out[-500:]}")


async def _tag_and_push(local_tag: str, remote_ref: str) -> None:
    code, out = await _run(["docker", "tag", local_tag, remote_ref], timeout=60)
    if code:
        raise TargetError(f"docker tag failed: {out[-300:]}")
    code, out = await _run(["docker", "push", remote_ref], timeout=1800)
    if code:
        raise TargetError(f"docker push to {remote_ref} failed:\n{out[-1500:]}")


# ── AWS ECR ───────────────────────────────────────────────────────────────────

async def _ecr_call(aws, op: str, payload: dict) -> dict:
    """ECR lives at api.ecr.<region> but SIGNS as service 'ecr' — the host
    override keeps the two straight (json_call would sign as 'api.ecr')."""
    r = await aws.request(
        "ecr", host=f"api.ecr.{aws.region}.amazonaws.com",
        body=payload, target=f"AmazonEC2ContainerRegistry_V20150921.{op}",
        json_version="1.1",
    )
    return r.json() if r.content else {}


async def ecr_push(aws, image_name: str, local_tag: str) -> str:
    """Ensure an ECR repository named `image_name` exists, authenticate, and
    push. `aws` is a targets.awslib.AwsClient. Returns the remote ref."""
    from .awslib import AwsError
    try:
        await _ecr_call(aws, "CreateRepository", {"repositoryName": image_name})
    except AwsError as e:
        if "RepositoryAlreadyExists" not in (e.code or ""):
            raise TargetError(f"ECR repository create failed: {e}") from e
    try:
        auth = await _ecr_call(aws, "GetAuthorizationToken", {})
        blob = auth["authorizationData"][0]
        token = base64.b64decode(blob["authorizationToken"]).decode()
        username, _, password = token.partition(":")
        endpoint = blob["proxyEndpoint"].removeprefix("https://")
    except (AwsError, KeyError, IndexError, ValueError) as e:
        raise TargetError(f"ECR auth failed: {e}") from e
    await _docker_login(endpoint, username, password)
    remote_ref = f"{endpoint}/{image_name}:latest"
    await _tag_and_push(local_tag, remote_ref)
    return remote_ref


# ── GCP Artifact Registry ─────────────────────────────────────────────────────

async def artifact_registry_push(gcp, region: str, image_name: str,
                                 local_tag: str) -> str:
    """Ensure the shared `homebox` docker repository exists in Artifact
    Registry, authenticate with the client's access token, and push. `gcp` is
    a targets.gcplib.GcpClient. Returns the remote ref."""
    from .gcplib import GcpError
    parent = f"projects/{gcp.project_id}/locations/{region}"
    try:
        await gcp.request(
            "POST",
            f"https://artifactregistry.googleapis.com/v1/{parent}/repositories",
            params={"repositoryId": GCP_REPO_ID},
            json={"format": "DOCKER",
                  "description": "Homebox cloud-target images"},
        )
    except GcpError as e:
        if e.status != 409:  # already exists
            raise TargetError(f"Artifact Registry repo create failed: {e}") from e
    registry = f"{region}-docker.pkg.dev"
    token = await gcp.token()
    await _docker_login(registry, "oauth2accesstoken", token)
    remote_ref = f"{registry}/{gcp.project_id}/{GCP_REPO_ID}/{image_name}:latest"
    await _tag_and_push(local_tag, remote_ref)
    return remote_ref
