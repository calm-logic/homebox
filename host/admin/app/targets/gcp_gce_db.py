"""GCP Compute Engine database-VM target — a cloud replica of a clustered
Postgres.

The GCE twin of aws_ec2_db: runs the SAME pgEdge Postgres container the
homebox cluster uses (app/cluster_db.py) on a Compute Engine instance that
joins the cluster's WireGuard mesh. The bootstrap is the shared cloud-init
script (db_vm_common.render_cloud_init), delivered via the `user-data`
metadata key, which cloud-init on the Ubuntu boot image executes on first
boot.

Deploy flow (idempotent):
  1. GET zones/{zone}/instances/{name} — an existing instance is adopted.
  2. Ensure the shared global firewall `homebox-db-mesh` targeting the
     `homebox-db` network tag: udp/51820 always, tcp/5432 only when
     open_pg_public. A 409 (already exists) is tolerated — first writer
     wins, matching EC2's duplicate-rule tolerance.
  3. POST the instance (Ubuntu 24.04 LTS boot disk, ephemeral external IP),
     poll the zone operation, then poll the instance until RUNNING with a
     natIP.

State persisted (same shapes as EC2 — targetslib.mesh_extra_peers reads
`mesh`, targetslib.db_vm_extra_nodes reads `db`):

    {instance_name, zone, project, public_ip,
     mesh: {ordinal, ip, wg_pubkey, endpoint: "<public_ip>:51820"},
     db:   {port: 5432, node_name: "n<ordinal>"}}
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

import httpx

from .base import DeployTarget, TargetDeployCtx, TargetError, TargetResult
from .db_vm_common import (
    WG_PORT,
    DbVmSpec,
    render_cloud_init,
    spec_from_config,
    vm_state_entries,
)
from .gcplib import GcpClient, GcpError

DEFAULT_REGION = "us-central1"
DEFAULT_MACHINE = "e2-small"
BOOT_IMAGE = "projects/ubuntu-os-cloud/global/images/family/ubuntu-2404-lts-amd64"
FIREWALL_NAME = "homebox-db-mesh"
NETWORK_TAG = "homebox-db"

# GCE instance names: RFC1035 — ≤63 chars, lowercase [a-z0-9-], must start
# with a letter and may not end with a dash.
_NAME_MAX = 63

# Operation/instance polling: ATTEMPTS × INTERVAL bounds the wait. The
# interval is a module constant so tests can zero it; the attempt count
# keeps the cap finite even then.
OP_POLL_INTERVAL = 3.0
OP_POLL_ATTEMPTS = 100

_DEAD_STATUSES = ("STOPPING", "TERMINATED", "SUSPENDING", "SUSPENDED")


def _instance_name(ctx: TargetDeployCtx) -> str:
    """Sanitize ctx.resource_name into a valid GCE instance name."""
    name = ctx.resource_name.lower()
    name = re.sub(r"[^a-z0-9-]+", "-", name)
    name = re.sub(r"-{2,}", "-", name).strip("-")
    if not name or not name[0].isalpha():
        name = "d-" + name  # names must start with a letter
    return name[:_NAME_MAX].rstrip("-")


def _nat_ip(inst: dict[str, Any]) -> str | None:
    for ni in inst.get("networkInterfaces") or []:
        for ac in ni.get("accessConfigs") or []:
            ip = ac.get("natIP")
            if ip:
                return str(ip)
    return None


class GceDbTarget(DeployTarget):
    """GCP Compute Engine database VM (pgEdge Postgres joining the mesh)."""

    provider = "gcp"
    variant = "gce_db"

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
        region = self._config.get("region") or DEFAULT_REGION
        self._zone: str = self._config.get("zone") or f"{region}-a"
        # injectable for tests (httpx.MockTransport)
        self._gcp: GcpClient | None = GcpClient(sa, transport=transport) if sa else None

    # ───── plumbing ───────────────────────────────────────────────────────────

    def _client(self) -> GcpClient:
        if self._gcp is None:
            raise TargetError(
                "GCP integration is missing its service-account key — "
                "reconnect the account in Integrations."
            )
        return self._gcp

    async def _get_instance(
        self, name: str, *, zone: str | None = None
    ) -> dict[str, Any] | None:
        """The instance resource, or None on 404."""
        gcp = self._client()
        try:
            r = await gcp.compute(
                "GET", f"zones/{zone or self._zone}/instances/{name}")
        except GcpError as e:
            if e.status == 404:
                return None
            raise
        return r.json() or {}

    async def _wait_zone_operation(self, op_name: str | None,
                                   ctx: TargetDeployCtx) -> None:
        if not op_name:
            return
        gcp = self._client()
        for _ in range(OP_POLL_ATTEMPTS):
            r = await gcp.compute(
                "GET", f"zones/{self._zone}/operations/{op_name}")
            op = r.json() or {}
            if op.get("status") == "DONE":
                err = op.get("error")
                if err:
                    msgs = "; ".join(
                        e.get("message") or e.get("code") or ""
                        for e in err.get("errors") or []
                    ) or str(err)
                    raise TargetError(f"GCE operation failed: {msgs}")
                return
            await asyncio.sleep(OP_POLL_INTERVAL)
        raise TargetError(
            f"GCE operation {op_name} did not complete within "
            f"~{int(OP_POLL_ATTEMPTS * OP_POLL_INTERVAL)}s"
        )

    async def _wait_running(
        self, name: str, ctx: TargetDeployCtx
    ) -> tuple[dict[str, Any], str]:
        """Poll the instance until RUNNING with an external IP."""
        last_status = ""
        for _ in range(OP_POLL_ATTEMPTS):
            inst = await self._get_instance(name)
            if inst is not None:
                status = str(inst.get("status") or "")
                ip = _nat_ip(inst)
                if status == "RUNNING" and ip:
                    return inst, ip
                if status in _DEAD_STATUSES:
                    raise TargetError(
                        f"GCE instance {name} ended in status {status} — "
                        "check its serial console in the GCP console."
                    )
                if status != last_status:
                    await ctx.emit(f"instance status: {status or '?'}…")
                    last_status = status
            await asyncio.sleep(OP_POLL_INTERVAL)
        raise TargetError(
            f"timed out waiting for GCE instance {name} to reach RUNNING "
            f"with an external IP (last status: {last_status or '?'})."
        )

    async def _ensure_firewall(self, spec: DbVmSpec,
                               ctx: TargetDeployCtx) -> None:
        """Ensure the shared mesh firewall exists. 409 = someone (an earlier
        DB VM deploy) already created it — first writer wins; a later VM
        wanting public 5432 should set open_pg_public before the first
        deploy or open the port manually."""
        gcp = self._client()
        allowed: list[dict[str, Any]] = [
            {"IPProtocol": "udp", "ports": [str(WG_PORT)]}]
        if spec.open_pg_public:
            allowed.append({"IPProtocol": "tcp", "ports": ["5432"]})
        body = {
            "name": FIREWALL_NAME,
            "network": "global/networks/default",
            "direction": "INGRESS",
            "sourceRanges": ["0.0.0.0/0"],
            "targetTags": [NETWORK_TAG],
            "allowed": allowed,
        }
        try:
            await gcp.compute("POST", "global/firewalls", json=body)
            await ctx.emit(f"created firewall {FIREWALL_NAME}")
        except GcpError as e:
            if e.status != 409:
                raise

    # ───── contract ───────────────────────────────────────────────────────────

    async def validate(self) -> None:
        gcp = self._client()
        try:
            await gcp.get_project()
        except GcpError as e:
            if e.status in (401, 403):
                raise TargetError(
                    f"The GCP service account cannot access project "
                    f"{gcp.project_id}. Grant it the Compute Admin role, "
                    f"then update the key in Integrations. ({e})"
                ) from e
            raise TargetError(f"GCP validation failed: {e}") from e
        except httpx.HTTPError as e:
            raise TargetError(f"GCP validation failed: {e}") from e

    async def deploy(self, ctx: TargetDeployCtx) -> TargetResult:
        spec = spec_from_config(ctx.config)
        wg_pubkey = str(ctx.config.get("wg_public_key") or "")
        gcp = self._client()
        name = _instance_name(ctx)
        zone = self._zone
        try:
            existing = await self._get_instance(name)
            if existing is None:
                await self._ensure_firewall(spec, ctx)
                machine = str(
                    ctx.config.get("instance_size") or DEFAULT_MACHINE)
                body = {
                    "name": name,
                    "machineType": f"zones/{zone}/machineTypes/{machine}",
                    "disks": [{
                        "boot": True,
                        "autoDelete": True,
                        "initializeParams": {"sourceImage": BOOT_IMAGE},
                    }],
                    "networkInterfaces": [{
                        "network": "global/networks/default",
                        "accessConfigs": [{
                            "type": "ONE_TO_ONE_NAT",
                            "name": "External NAT",
                        }],
                    }],
                    # cloud-init on the Ubuntu image executes `user-data`.
                    "metadata": {"items": [{
                        "key": "user-data",
                        "value": render_cloud_init(spec),
                    }]},
                    "tags": {"items": [NETWORK_TAG]},
                }
                await ctx.emit(f"creating GCE instance {name} in {zone}…")
                r = await gcp.compute(
                    "POST", f"zones/{zone}/instances", json=body)
                await self._wait_zone_operation((r.json() or {}).get("name"), ctx)
            else:
                await ctx.emit(f"GCE instance {name} already exists — adopting it")
            inst, public_ip = await self._wait_running(name, ctx)
        except GcpError as e:
            raise TargetError(f"GCE DB VM deploy failed: {e}") from e
        except httpx.HTTPError as e:
            raise TargetError(f"GCE DB VM deploy failed: {e}") from e
        await ctx.emit(
            f"database VM running at {public_ip} (mesh {spec.mesh_ip}, "
            f"spock node {spec.node_name})"
        )

        # No hostname/DNS: databases are backing services reached over the
        # mesh (or directly on 5432 when open_pg_public).
        return TargetResult(
            endpoint=public_ip,
            cname_target=None,
            proxied=False,
            state={
                "instance_name": name,
                "zone": zone,
                "project": gcp.project_id,
                "public_ip": public_ip,
                **vm_state_entries(
                    spec, wg_public_key=wg_pubkey, public_ip=public_ip),
            },
        )

    async def destroy(self, state: dict[str, Any]) -> None:
        state = state or {}
        name = state.get("instance_name")
        if not name:
            return
        gcp = self._client()
        zone = state.get("zone") or self._zone
        try:
            await gcp.compute("DELETE", f"zones/{zone}/instances/{name}")
        except GcpError as e:
            if e.status == 404:
                return  # Already gone — fine.
            raise TargetError(f"GCE DB VM destroy failed: {e}") from e
        except httpx.HTTPError as e:
            raise TargetError(f"GCE DB VM destroy failed: {e}") from e

    async def probe(self, state: dict[str, Any]) -> bool:
        state = state or {}
        name = state.get("instance_name")
        if not name or self._gcp is None:
            return False
        try:
            inst = await self._get_instance(
                str(name), zone=state.get("zone"))
        except (GcpError, httpx.HTTPError):
            return False
        return inst is not None and inst.get("status") == "RUNNING"
