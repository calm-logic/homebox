"""AWS EC2 database-VM target — a cloud replica of a clustered Postgres.

Runs the SAME pgEdge Postgres container the homebox cluster uses (see
app/cluster_db.py) on a dedicated EC2 instance that joins the cluster's
WireGuard mesh. All the interesting work happens in cloud-init
(db_vm_common.render_cloud_init); this module only drives EC2's Query
protocol via awslib.AwsClient.ec2 (XML responses).

Deploy flow (idempotent — a coordinator handover must converge):
  1. DescribeInstances filtered on tag homebox-db=<resource_name> in state
     pending|running — an existing instance is adopted, never duplicated.
  2. Ensure the security group homebox-db-<resource_name>: udp/51820 open to
     the world always (WireGuard authenticates by key), tcp/5432 to the
     world only when open_pg_public (serverless consumers), else one rule
     per config.allowed_cidrs. Duplicate-group/-rule errors are tolerated.
  3. RunInstances with base64 user data + tags, then poll until the
     instance is running with a public IP.

State persisted (targetslib.mesh_extra_peers reads `mesh`,
targetslib.db_vm_extra_nodes reads `db` — shapes come from
db_vm_common.vm_state_entries):

    {instance_id, sg_id, public_ip,
     mesh: {ordinal, ip, wg_pubkey, endpoint: "<public_ip>:51820"},
     db:   {port: 5432, node_name: "n<ordinal>"}}
"""

from __future__ import annotations

import asyncio
import base64
import logging
import xml.etree.ElementTree as ET
from typing import Any

import httpx

from .awslib import AwsClient, AwsError, _find_text, _local
from .base import DeployTarget, TargetDeployCtx, TargetError, TargetResult
from .db_vm_common import (
    WG_PORT,
    DbVmSpec,
    render_cloud_init,
    spec_from_config,
    vm_state_entries,
)

log = logging.getLogger("homebox.targets.ec2_db")

_TAG_KEY = "homebox-db"
_DEFAULT_INSTANCE_TYPE = "t3.small"

# Instance polling knobs — module constants so tests can zero the interval.
_POLL_INTERVAL = 5.0
_POLL_TIMEOUT = 600.0

_DEAD_STATES = ("shutting-down", "terminated", "stopping", "stopped")


# ───── XML helpers (EC2's namespaced-but-inconsistent responses) ──────────────


def _instance_items(root: ET.Element) -> list[ET.Element]:
    """Every <item> element that IS an instance (direct <instanceId> child)
    — works for both DescribeInstances reservations and RunInstances."""
    return [
        el for el in root.iter()
        if _local(el.tag) == "item"
        and any(_local(c.tag) == "instanceId" for c in el)
    ]


def _instance_state_name(inst: ET.Element) -> str:
    for el in inst.iter():
        if _local(el.tag) == "instanceState":
            for child in el:
                if _local(child.tag) == "name":
                    return (child.text or "").strip()
    return ""


def _first_group_id(inst: ET.Element) -> str | None:
    for el in inst.iter():
        if _local(el.tag) == "groupId":
            return (el.text or "").strip() or None
    return None


class Ec2DbTarget(DeployTarget):
    """AWS EC2 database VM (pgEdge Postgres joining the cluster mesh)."""

    provider = "aws"
    variant = "ec2_db"

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
        self._aws = AwsClient(
            creds.get("key_id") or "",
            creds.get("secret") or "",
            creds.get("region") or "us-east-1",
            transport=transport,  # injectable for tests (httpx.MockTransport)
        )

    # ───── EC2 steps ──────────────────────────────────────────────────────────

    async def _find_tagged_instance(self, name: str) -> ET.Element | None:
        """The live (pending|running) instance carrying our deterministic tag,
        or None. Terminated instances keep their tags for a while — the state
        filter keeps a destroy-then-redeploy from adopting a corpse."""
        root = await self._aws.ec2("DescribeInstances", {
            "Filter.1.Name": f"tag:{_TAG_KEY}",
            "Filter.1.Value.1": name,
            "Filter.2.Name": "instance-state-name",
            "Filter.2.Value.1": "pending",
            "Filter.2.Value.2": "running",
        })
        items = _instance_items(root)
        return items[0] if items else None

    async def _ensure_security_group(
        self, ctx: TargetDeployCtx, spec: DbVmSpec
    ) -> str:
        sg_name = f"homebox-db-{ctx.resource_name}"
        try:
            root = await self._aws.ec2("CreateSecurityGroup", {
                "GroupName": sg_name,
                "GroupDescription": (
                    f"homebox DB VM {ctx.resource_name}: WireGuard mesh + Postgres"
                ),
            })
            sg_id = _find_text(root, "groupId") or ""
            await ctx.emit(f"created security group {sg_name} ({sg_id})")
        except AwsError as e:
            if e.code != "InvalidGroup.Duplicate":
                raise
            root = await self._aws.ec2("DescribeSecurityGroups", {
                "Filter.1.Name": "group-name",
                "Filter.1.Value.1": sg_name,
            })
            sg_id = _find_text(root, "groupId") or ""
        if not sg_id:
            raise TargetError(
                f"could not create or locate the security group {sg_name}."
            )

        # WireGuard is always world-open (key-authenticated); 5432 is public
        # only for serverless consumers, else restricted to allowed_cidrs.
        rules: list[tuple[str, int, str]] = [("udp", WG_PORT, "0.0.0.0/0")]
        if spec.open_pg_public:
            rules.append(("tcp", 5432, "0.0.0.0/0"))
        else:
            for cidr in ctx.config.get("allowed_cidrs") or []:
                rules.append(("tcp", 5432, str(cidr)))
        # One rule per call: a duplicate must not swallow its siblings.
        for proto, port, cidr in rules:
            try:
                await self._aws.ec2("AuthorizeSecurityGroupIngress", {
                    "GroupId": sg_id,
                    "IpPermissions.1.IpProtocol": proto,
                    "IpPermissions.1.FromPort": str(port),
                    "IpPermissions.1.ToPort": str(port),
                    "IpPermissions.1.IpRanges.1.CidrIp": cidr,
                })
            except AwsError as e:
                if e.code != "InvalidPermission.Duplicate":
                    raise
        return sg_id

    async def _run_instance(
        self, ctx: TargetDeployCtx, spec: DbVmSpec, ami: str, sg_id: str
    ) -> str:
        name = ctx.resource_name
        user_data = base64.b64encode(render_cloud_init(spec).encode()).decode()
        root = await self._aws.ec2("RunInstances", {
            "ImageId": ami,
            "InstanceType": str(
                ctx.config.get("instance_size") or _DEFAULT_INSTANCE_TYPE),
            "MinCount": "1",
            "MaxCount": "1",
            "UserData": user_data,
            "SecurityGroupId.1": sg_id,
            "TagSpecification.1.ResourceType": "instance",
            "TagSpecification.1.Tag.1.Key": _TAG_KEY,
            "TagSpecification.1.Tag.1.Value": name,
            "TagSpecification.1.Tag.2.Key": "Name",
            "TagSpecification.1.Tag.2.Value": name,
        })
        items = _instance_items(root)
        instance_id = _find_text(items[0], "instanceId") if items else None
        if not instance_id:
            raise TargetError("EC2 RunInstances returned no instance id.")
        await ctx.emit(f"launched EC2 instance {instance_id}")
        return instance_id

    async def _describe_instance(self, instance_id: str) -> ET.Element | None:
        root = await self._aws.ec2(
            "DescribeInstances", {"InstanceId.1": instance_id})
        items = _instance_items(root)
        return items[0] if items else None

    async def _wait_running(
        self, instance_id: str, ctx: TargetDeployCtx
    ) -> tuple[ET.Element, str]:
        """Poll DescribeInstances until running with a public IP."""
        waited = 0.0
        last_state = ""
        while True:
            inst = await self._describe_instance(instance_id)
            state = _instance_state_name(inst) if inst is not None else ""
            ip = _find_text(inst, "ipAddress") if inst is not None else None
            if state == "running" and ip:
                return inst, ip
            if state in _DEAD_STATES:
                raise TargetError(
                    f"EC2 instance {instance_id} ended in state {state} — "
                    "check the instance's console output in the AWS console."
                )
            if state != last_state:
                await ctx.emit(f"instance state: {state or '?'}…")
                last_state = state
            if waited >= _POLL_TIMEOUT:
                raise TargetError(
                    f"timed out after {int(_POLL_TIMEOUT)}s waiting for EC2 "
                    f"instance {instance_id} to reach running with a public "
                    f"IP (last state: {state or '?'})."
                )
            await asyncio.sleep(_POLL_INTERVAL)
            # A zeroed interval (tests) still counts toward the timeout so
            # the loop stays bounded.
            waited += _POLL_INTERVAL if _POLL_INTERVAL > 0 else 5.0

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
        spec = spec_from_config(ctx.config)
        wg_pubkey = str(ctx.config.get("wg_public_key") or "")
        name = ctx.resource_name
        try:
            existing = await self._find_tagged_instance(name)
            if existing is not None:
                instance_id = _find_text(existing, "instanceId") or ""
                sg_id = (_first_group_id(existing)
                         or str(self._state.get("sg_id") or ""))
                await ctx.emit(
                    f"EC2 instance {instance_id} already exists — adopting it")
            else:
                ami = str(ctx.config.get("ami") or "")
                if not ami:
                    raise TargetError(
                        "EC2 database VMs need config.ami — AMI ids are "
                        "region-specific (pick an Ubuntu 24.04 LTS or Amazon "
                        "Linux 2023 image for this integration's region)."
                    )
                sg_id = await self._ensure_security_group(ctx, spec)
                instance_id = await self._run_instance(ctx, spec, ami, sg_id)
            inst, public_ip = await self._wait_running(instance_id, ctx)
            if not sg_id:
                sg_id = _first_group_id(inst) or ""
        except AwsError as e:
            raise TargetError(f"EC2 DB VM deploy failed: {e}") from e
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
                "instance_id": instance_id,
                "sg_id": sg_id,
                "public_ip": public_ip,
                **vm_state_entries(
                    spec, wg_public_key=wg_pubkey, public_ip=public_ip),
            },
        )

    async def destroy(self, state: dict[str, Any]) -> None:
        state = state or {}
        instance_id = state.get("instance_id")
        if instance_id:
            try:
                await self._aws.ec2(
                    "TerminateInstances", {"InstanceId.1": str(instance_id)})
            except AwsError as e:
                code = e.code or ""
                if not code.startswith("InvalidInstanceID"):
                    raise TargetError(f"EC2 DB VM destroy failed: {e}") from e
        sg_id = state.get("sg_id")
        if sg_id:
            # Best-effort: the group stays referenced until the instance's
            # ENIs detach (DependencyViolation), and a re-run may find it
            # already gone. Neither should fail the destroy.
            try:
                await self._aws.ec2(
                    "DeleteSecurityGroup", {"GroupId": str(sg_id)})
            except AwsError as e:
                log.info("leaving security group %s behind: %s", sg_id, e)

    async def probe(self, state: dict[str, Any]) -> bool:
        instance_id = (state or {}).get("instance_id")
        if not instance_id:
            return False
        try:
            inst = await self._describe_instance(str(instance_id))
        except AwsError:
            return False
        return inst is not None and _instance_state_name(inst) == "running"
