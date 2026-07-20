"""GCP Cloud Run deployment target — container services via the Admin API v2.

Flow per deploy:
  1. Push the locally-built image to Artifact Registry (Cloud Run can only
     pull from Artifact Registry / GCR). For image-origin services ctx.image
     is an upstream ref (e.g. postgres:16) that may not exist locally, so we
     `docker pull` it first — tolerating a pull failure because the tag may
     be a local build, in which case the `docker tag` inside
     registry.artifact_registry_push succeeds anyway (and fails loudly if the
     image exists nowhere).
  2. Create-or-update the service (GET → 404 → POST create, else PATCH) at
     projects/{project}/locations/{region}/services/{service_id}. Both writes
     return a long-running operation which we poll to completion.
  3. Open public access: setIamPolicy granting roles/run.invoker to allUsers.
  4. Report the service's run.app URI as the endpoint.
  5. When the service has a hostname (and config.domain_mapping isn't False),
     map the custom domain: Google Site Verification of the hostname via a
     DNS TXT record (handed to the orchestrator through
     state.extra_dns_records — this module never writes DNS itself), then a
     Cloud Run domain mapping on the v1 (Knative) regional surface. While
     either step is still pending the deploy falls back to the run.app URL
     (cname_target=None → no DNS record) and records
     state.domain_mapping = "pending_verification" | "pending_mapping" so
     the reconcile loop retries next cycle; once the mapping serves
     resourceRecords, cname_target is its CNAME rrdata
     (ghs.googlehosted.com) with proxied=False and
     state.domain_mapping = "ready".
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

import httpx

from . import registry
from .base import DeployTarget, TargetDeployCtx, TargetError, TargetResult
from .gcplib import DEFAULT_SCOPE, GcpClient, GcpError
from .registry import _run

DEFAULT_REGION = "us-central1"

# Google Site Verification API — proves domain ownership before Cloud Run
# accepts a domain mapping. Needs its own OAuth scope on top of gcplib's
# default cloud-platform scope.
SITEVERIFICATION_API = "https://www.googleapis.com/siteVerification/v1"
SITEVERIFICATION_SCOPE = "https://www.googleapis.com/auth/siteverification"

# Cloud Run service ids: ≤63 chars, lowercase [a-z0-9-], must start with a
# letter and may not end with a dash.
_ID_MAX = 63

# Long-running operation polling: ATTEMPTS × INTERVAL ≈ 120s cap. The
# interval is a module constant so tests can zero it; the attempt count keeps
# the cap finite even then.
OP_POLL_INTERVAL = 3.0
OP_POLL_ATTEMPTS = 40

# Domain-mapping polling: a fresh mapping usually surfaces its
# resourceRecords within seconds, but certificate provisioning can leave it
# pending much longer — we poll briefly and otherwise fall back to the
# run.app URL (state "pending_mapping", retried next reconcile cycle).
# Module constants so tests can zero the interval.
MAPPING_POLL_INTERVAL = 3.0
MAPPING_POLL_ATTEMPTS = 20


def _siteverification_error(e: GcpError) -> TargetError:
    return TargetError(
        f"Google site verification failed: {e} — make sure the Site "
        "Verification API is enabled on the project (gcloud services enable "
        "siteverification.googleapis.com) and the service account key was "
        "saved after this Homebox version added the siteverification scope."
    )


def _service_id(ctx: TargetDeployCtx) -> str:
    """Sanitize ctx.resource_name into a valid Cloud Run service id."""
    name = ctx.resource_name.lower()
    name = re.sub(r"[^a-z0-9-]+", "-", name)
    name = re.sub(r"-{2,}", "-", name).strip("-")
    if not name or not name[0].isalpha():
        name = "s-" + name  # ids must start with a letter
    return name[:_ID_MAX].rstrip("-")


class CloudRunTarget(DeployTarget):
    """Google Cloud Run (web/api container services)."""

    provider = "gcp"
    variant = "cloud_run"

    def __init__(
        self,
        *,
        creds: dict[str, Any],
        config: dict[str, Any] | None = None,
        state: dict[str, Any] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        creds = creds or {}
        sa = creds.get("sa") or {}
        self._config = config or {}
        self._state = state or {}
        self._region: str = self._config.get("region") or DEFAULT_REGION
        # injectable for tests (httpx.MockTransport)
        # gcplib defaults to the cloud-platform scope only; the Site
        # Verification API needs its own scope, added via GcpClient's
        # `scopes` parameter — one token covers both APIs.
        self._gcp: GcpClient | None = (
            GcpClient(
                sa,
                scopes=[DEFAULT_SCOPE, SITEVERIFICATION_SCOPE],
                transport=transport,
            )
            if sa
            else None
        )

    # ───── plumbing ───────────────────────────────────────────────────────────

    def _client(self) -> GcpClient:
        if self._gcp is None:
            raise TargetError(
                "GCP integration is missing its service-account key — "
                "reconnect the account in Integrations."
            )
        return self._gcp

    def _service_path(self, service_id: str, *, region: str | None = None,
                      project: str | None = None) -> str:
        gcp = self._client()
        return (
            f"projects/{project or gcp.project_id}/locations/"
            f"{region or self._region}/services/{service_id}"
        )

    async def _wait_operation(self, name: str) -> dict:
        """Poll a Cloud Run long-running operation until done (≈120s cap)."""
        gcp = self._client()
        for _ in range(OP_POLL_ATTEMPTS):
            r = await gcp.run("GET", name)
            op = r.json()
            if op.get("done"):
                err = op.get("error")
                if err:
                    raise TargetError(
                        f"Cloud Run operation failed: "
                        f"{err.get('message') or err}"
                    )
                return op
            await asyncio.sleep(OP_POLL_INTERVAL)
        raise TargetError(
            f"Cloud Run operation {name} did not complete within "
            f"~{int(OP_POLL_ATTEMPTS * OP_POLL_INTERVAL)}s"
        )

    # ───── custom domain: site verification + domain mapping ──────────────────

    def _mappings_url(self, *, region: str | None = None,
                      project: str | None = None) -> str:
        """Collection URL for domain mappings. These live on the v1
        (Knative) regional surface — NOT the Run Admin v2 API that
        gcp.run() targets — so we build the full URL and go through
        gcp.request() directly."""
        gcp = self._client()
        return (
            f"https://{region or self._region}-run.googleapis.com/apis/"
            f"domains.cloudrun.com/v1/namespaces/"
            f"{project or gcp.project_id}/domainmappings"
        )

    async def _get_mapping(self, url: str) -> dict | None:
        """GET one domain mapping; None on 404."""
        try:
            r = await self._client().request("GET", url)
        except GcpError as e:
            if e.status == 404:
                return None
            raise
        return r.json() or {}

    @staticmethod
    def _mapping_cname(mapping: dict) -> str | None:
        """CNAME rrdata from status.resourceRecords (typically
        "ghs.googlehosted.com." — the trailing dot is stripped), or None
        while the mapping is still provisioning."""
        records = (mapping.get("status") or {}).get("resourceRecords") or []
        for rec in records:
            if rec.get("type") == "CNAME" and rec.get("rrdata"):
                return str(rec["rrdata"]).rstrip(".")
        return None

    async def _ensure_site_verified(
        self, ctx: TargetDeployCtx, hostname: str, state: dict[str, Any]
    ) -> bool:
        """Google Site Verification via DNS TXT. Returns True once the
        domain is verified; False (NOT an error) while the TXT record has
        not propagated yet — the reconcile loop retries next cycle."""
        gcp = self._client()
        # We verify the exact hostname as an INET_DOMAIN rather than the
        # registrable domain: the orchestrator can always write a TXT at the
        # per-host name it already controls, and verifying the host also
        # authorizes mapping it — no zone-apex ownership required.
        site = {"type": "INET_DOMAIN", "identifier": hostname}
        try:
            r = await gcp.request(
                "POST",
                f"{SITEVERIFICATION_API}/token",
                json={"site": site, "verificationMethod": "DNS_TXT"},
            )
        except GcpError as e:
            raise _siteverification_error(e) from e
        token = (r.json() or {}).get("token") or ""
        # Re-emitted on EVERY deploy while unverified; the orchestrator
        # upserts the record into DNS, so repeating it is idempotent.
        state["extra_dns_records"] = [
            {"type": "TXT", "name": hostname, "value": token}
        ]
        try:
            await gcp.request(
                "POST",
                f"{SITEVERIFICATION_API}/webResource",
                params={"verificationMethod": "DNS_TXT"},
                json={"site": site},
            )
        except GcpError as e:
            if e.status == 403:
                raise _siteverification_error(e) from e
            # DNS not propagated yet (or a transient failure) — not an
            # error: fall back to the run.app URL and retry next cycle.
            state["domain_mapping"] = "pending_verification"
            await ctx.emit(
                f"{hostname} is not verified with Google yet — the TXT "
                "record was handed to DNS; verification is retried on the "
                "next reconcile cycle (the run.app URL stays active)."
            )
            return False
        return True

    async def _ensure_domain_mapping(
        self, ctx: TargetDeployCtx, service_id: str, state: dict[str, Any]
    ) -> str | None:
        """Verify ctx.hostname and map it onto the Cloud Run service.
        Returns the CNAME target once the mapping serves resourceRecords,
        else None (run.app fallback; state says why so reconcile retries)."""
        hostname = ctx.hostname or ""
        state["hostname"] = hostname
        prior = (self._state or {}).get("domain_mapping")
        base = self._mappings_url()
        map_url = f"{base}/{hostname}"

        if prior == "ready":
            # Short-circuit: the mapping was live before — just confirm it
            # still exists (404 → fall through and recreate below).
            mapping = await self._get_mapping(map_url)
            if mapping is not None:
                cname = (
                    self._mapping_cname(mapping)
                    or self._state.get("mapping_cname")
                )
                if cname:
                    state["domain_mapping"] = "ready"
                    state["mapping_cname"] = cname
                    return cname

        # Site verification only has to succeed once per domain — prior
        # states "pending_mapping"/"ready" mean it already did.
        if prior not in ("ready", "pending_mapping"):
            if not await self._ensure_site_verified(ctx, hostname, state):
                return None

        gcp = self._client()
        mapping = await self._get_mapping(map_url)
        if mapping is None:
            await ctx.emit(f"creating Cloud Run domain mapping {hostname}…")
            try:
                r = await gcp.request(
                    "POST",
                    base,
                    json={
                        "apiVersion": "domains.cloudrun.com/v1",
                        "kind": "DomainMapping",
                        "metadata": {
                            "name": hostname,
                            "namespace": gcp.project_id,
                        },
                        "spec": {
                            "routeName": service_id,
                            "certificateMode": "AUTOMATIC",
                        },
                    },
                )
                mapping = r.json() or {}
            except GcpError as e:
                if e.status != 409:  # 409: racing deploy created it — fine
                    raise
                mapping = None

        for _ in range(MAPPING_POLL_ATTEMPTS):
            cname = self._mapping_cname(mapping or {})
            if cname:
                state["domain_mapping"] = "ready"
                state["mapping_cname"] = cname
                await ctx.emit(
                    f"domain mapping ready — CNAME {hostname} → {cname}"
                )
                return cname
            await asyncio.sleep(MAPPING_POLL_INTERVAL)
            mapping = await self._get_mapping(map_url)

        # Mappings can sit pending for a while (e.g. certificate
        # provisioning) — treat like unpropagated verification: fall back to
        # the run.app URL and let the reconcile loop retry.
        state["domain_mapping"] = "pending_mapping"
        await ctx.emit(
            f"domain mapping for {hostname} is still provisioning — falling "
            "back to the run.app URL; retried on the next reconcile cycle."
        )
        return None

    async def _ensure_local_image(self, image: str, ctx: TargetDeployCtx) -> None:
        """Pull upstream refs (postgres:16, …) that may not exist locally.
        A pull failure is tolerated: locally-built tags aren't pullable, and
        the docker tag inside artifact_registry_push fails loudly if the
        image truly exists nowhere."""
        code, out = await _run(["docker", "pull", image], timeout=900)
        if code:
            await ctx.emit(
                f"docker pull {image} failed (assuming a locally-built tag)"
            )

    # ───── contract ───────────────────────────────────────────────────────────

    async def validate(self) -> None:
        gcp = self._client()
        try:
            await gcp.get_project()
        except GcpError as e:
            if e.status in (401, 403):
                raise TargetError(
                    f"The GCP service account cannot access project "
                    f"{gcp.project_id}. Grant it the Cloud Run Admin, Artifact "
                    f"Registry Writer and Service Account User roles, then "
                    f"update the key in Integrations. ({e})"
                ) from e
            raise TargetError(f"GCP validation failed: {e}") from e
        except httpx.HTTPError as e:
            raise TargetError(f"GCP validation failed: {e}") from e

    async def deploy(self, ctx: TargetDeployCtx) -> TargetResult:
        if not ctx.image:
            raise TargetError(
                "Cloud Run needs a container image, but this service produced "
                "none — check the service's build output."
            )
        gcp = self._client()
        region = self._region
        service_id = _service_id(ctx)
        try:
            # 1. Push the image where Cloud Run can pull it.
            await ctx.emit(f"pushing image {ctx.image} to Artifact Registry…")
            await self._ensure_local_image(ctx.image, ctx)
            remote_ref = await registry.artifact_registry_push(
                gcp, region, image_name=service_id, local_tag=ctx.image
            )

            # 2. Create-or-update the service.
            parent = f"projects/{gcp.project_id}/locations/{region}"
            svc_path = f"{parent}/services/{service_id}"
            body = {
                "template": {
                    "containers": [
                        {
                            "image": remote_ref,
                            "ports": [
                                {"containerPort": ctx.internal_port or 8080}
                            ],
                            "env": [
                                {"name": k, "value": v}
                                for k, v in ctx.env_vars.items()
                            ],
                        }
                    ],
                    "scaling": {"minInstanceCount": 0, "maxInstanceCount": 3},
                },
                "ingress": "INGRESS_TRAFFIC_ALL",
            }
            exists = True
            try:
                await gcp.run("GET", svc_path)
            except GcpError as e:
                if e.status != 404:
                    raise
                exists = False
            if exists:
                await ctx.emit(f"updating Cloud Run service {service_id}…")
                r = await gcp.run("PATCH", svc_path, json=body)
            else:
                await ctx.emit(f"creating Cloud Run service {service_id}…")
                r = await gcp.run(
                    "POST",
                    f"{parent}/services",
                    params={"serviceId": service_id},
                    json=body,
                )
            op_name = (r.json() or {}).get("name")
            if op_name:
                await ctx.emit("waiting for Cloud Run operation…")
                await self._wait_operation(op_name)

            # 3. Open public access.
            await gcp.run(
                "POST",
                f"{svc_path}:setIamPolicy",
                json={
                    "policy": {
                        "bindings": [
                            {
                                "role": "roles/run.invoker",
                                "members": ["allUsers"],
                            }
                        ]
                    }
                },
            )

            # 4. The endpoint is the service's run.app URI.
            r = await gcp.run("GET", svc_path)
            uri = (r.json() or {}).get("uri") or ""
        except GcpError as e:
            raise TargetError(f"Cloud Run deploy failed: {e}") from e
        except httpx.HTTPError as e:
            raise TargetError(f"Cloud Run deploy failed: {e}") from e
        if not uri:
            raise TargetError(
                f"Cloud Run service {service_id} deployed but reported no URI."
            )
        endpoint = re.sub(r"^https?://", "", uri).rstrip("/")
        await ctx.emit(f"Cloud Run service {service_id} live at {uri}")

        state: dict[str, Any] = {
            "service_id": service_id,
            "region": region,
            "project": gcp.project_id,
            "uri": uri,
            "image": remote_ref,
        }
        # Until a domain mapping is live, cname_target=None → the
        # orchestrator writes no DNS record and surfaces the run.app URL
        # (the fallback while verification/mapping is pending).
        cname_target: str | None = None
        proxied = True
        if ctx.hostname and self._config.get("domain_mapping", True):
            try:
                cname_target = await self._ensure_domain_mapping(
                    ctx, service_id, state
                )
            except GcpError as e:
                hint = (
                    " — grant the service account permission to manage Cloud "
                    "Run domain mappings (roles/run.admin)"
                    if e.status == 403
                    else ""
                )
                raise TargetError(
                    f"Cloud Run domain mapping failed: {e}{hint}"
                ) from e
            except httpx.HTTPError as e:
                raise TargetError(
                    f"Cloud Run domain mapping failed: {e}"
                ) from e
            if cname_target:
                # proxied MUST stay False: Google's managed-certificate
                # provisioning resolves the hostname and needs to see the
                # CNAME to ghs.googlehosted.com directly — a Cloudflare
                # orange-cloud proxy answers with Cloudflare's own edge IPs
                # and breaks cert issuance.
                proxied = False

        return TargetResult(
            endpoint=endpoint,
            cname_target=cname_target,
            proxied=proxied,
            state=state,
        )

    async def destroy(self, state: dict[str, Any]) -> None:
        state = state or {}
        service_id = state.get("service_id")
        if not service_id:
            return
        gcp = self._client()
        # Remove the domain mapping first (it references the service). Only
        # states that actually created one ("pending_mapping"/"ready") carry
        # it; 404 = already gone, which is fine.
        hostname = state.get("hostname")
        if hostname and state.get("domain_mapping") in ("pending_mapping", "ready"):
            map_url = (
                self._mappings_url(
                    region=state.get("region"), project=state.get("project")
                )
                + f"/{hostname}"
            )
            try:
                await gcp.request("DELETE", map_url)
            except GcpError as e:
                if e.status != 404:
                    raise TargetError(
                        f"Cloud Run domain-mapping delete failed: {e}"
                    ) from e
            except httpx.HTTPError as e:
                raise TargetError(
                    f"Cloud Run domain-mapping delete failed: {e}"
                ) from e
        path = self._service_path(
            service_id, region=state.get("region"), project=state.get("project")
        )
        try:
            r = await gcp.run("DELETE", path)
        except GcpError as e:
            if e.status == 404:
                return  # Already gone — fine.
            raise TargetError(f"Cloud Run destroy failed: {e}") from e
        except httpx.HTTPError as e:
            raise TargetError(f"Cloud Run destroy failed: {e}") from e
        # Poll the delete operation briefly (best-effort — the DELETE was
        # accepted, so an unfinished or vanished operation is not an error).
        op_name = (r.json() or {}).get("name") if r.content else None
        if not op_name:
            return
        try:
            for _ in range(10):
                r = await gcp.run("GET", op_name)
                if r.json().get("done"):
                    return
                await asyncio.sleep(OP_POLL_INTERVAL)
        except (GcpError, httpx.HTTPError):
            return

    async def probe(self, state: dict[str, Any]) -> bool:
        state = state or {}
        service_id = state.get("service_id")
        if not service_id or self._gcp is None:
            return False
        path = self._service_path(
            service_id, region=state.get("region"), project=state.get("project")
        )
        try:
            r = await self._gcp.run("GET", path)
        except (GcpError, httpx.HTTPError):
            return False
        svc = r.json() or {}
        terminal = svc.get("terminalCondition") or {}
        return (
            terminal.get("state") == "CONDITION_SUCCEEDED"
            or bool(svc.get("latestReadyRevision"))
        )
