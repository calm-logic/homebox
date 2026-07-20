"""Cloud node provisioner (linked-accounts D6): boot a FULL Homebox node on
the user's linked AWS or GCP account that auto-joins the current cluster.

The flow runs on a homebox node — never the control plane (the CP never holds
cloud credentials):

  1. Validate: linked account + active cluster (the VM needs a cluster to
     join) + a matching aws/gcp Integration row.
  2. Mint a join token at the control plane — the exact same call
     routes/cluster.py POST /join-token makes (clusterlib._cp against
     /v1/clusters/{id}/join-tokens with the account token).
  3. Render a startup script adapted from the cloud-mirror provisioner's
     startup-script.sh.tpl: install Docker, pre-seed admin secrets.json (so
     the script knows the plaintext admin password), run the Homebox
     installer, then POST /api/cluster/join with the minted token and a
     peer_url derived from the VM's public IP. Unlike the mirror template it
     does NOT set HOMEBOX_NODE_ROLE=mirror — this is a full peer.
  4. Create the VM via targets/awslib (EC2 Query protocol) or targets/gcplib
     (compute/v1), the same way targets/aws_ec2_db.py / gcp_gce_db.py do:
     deterministic name "homebox-node-<name>", idempotent adopt-by-tag/name,
     small default machine type, security group / firewall for the ports a
     homebox peer needs (tcp/80 peer API, tcp/443, udp/51820 WireGuard).

State lives in the settings KV table under "node_provisions" — a list of

    {id, name, provider, integration_id, region, machine,
     status: creating | booting | joined | error,
     node_id?, error?, created_at,
     roster_before: [node ids at provision time],
     resource: {instance_id, sg_id | instance_name, zone, project}}

refresh_provisions() is called lazily from the GET route (no background
loop): it detects a join by looking for a NEW roster node whose name matches
the provision, checks the instance's state via the cloud API, and marks
entries error on instance death or a 30-minute timeout. Teardown terminates
the VM idempotently (like the db_vm destroy paths) and never auto-evicts a
joined node — the user evicts via the roster.
"""

from __future__ import annotations

import json
import logging
import re
import secrets as _secrets
import shlex
from datetime import datetime, timedelta
from typing import Any

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from . import clusterlib, crypto
from .config import settings
from .models import Integration
from .targets.awslib import AwsClient, AwsError, _find_text, _local
from .targets.gcplib import GcpClient, GcpError

log = logging.getLogger("homebox.nodeprovision")

PROVISIONS_KEY = "node_provisions"

_TAG_KEY = "homebox-node"
_AWS_DEFAULT_MACHINE = "t3.small"
_GCP_DEFAULT_MACHINE = "e2-small"
_GCE_BOOT_IMAGE = (
    "projects/ubuntu-os-cloud/global/images/family/ubuntu-2404-lts-amd64"
)
_GCE_FIREWALL_NAME = "homebox-node-mesh"
_GCE_NETWORK_TAG = "homebox-node"
# Canonical (Ubuntu) AMI owner; EC2 AMI ids are region-specific so the
# newest Ubuntu 24.04 LTS image is resolved per region via DescribeImages.
_UBUNTU_OWNER = "099720109477"
_UBUNTU_NAME_FILTER = "ubuntu/images/hvm-ssd*/ubuntu-noble-24.04-amd64-server-*"

# A node that hasn't joined within this window is marked error.
PROVISION_TIMEOUT = timedelta(minutes=30)

_EC2_DEAD_STATES = ("shutting-down", "terminated", "stopping", "stopped")
_GCE_DEAD_STATUSES = ("STOPPING", "TERMINATED", "SUSPENDING", "SUSPENDED")

# Injectable transport (httpx.MockTransport) so route-level tests can fake
# the cloud APIs without touching the module's callers.
_TRANSPORT: httpx.AsyncBaseTransport | None = None


class NodeProvisionError(Exception):
    """A provisioning step failed. `status` is the HTTP status the routes
    should surface (412 preconditions, 404 unknown ids, 502 cloud errors)."""

    def __init__(self, message: str, *, status: int = 400):
        super().__init__(message)
        self.status = status


# ───── startup script ─────────────────────────────────────────────────────────
#
# Adapted from cloud/mirror/provisioner/startup-script.sh.tpl, minus the
# mirror role and the provisioner callback (join detection is roster-driven).
# The rendered parameters are substituted with shlex-quoted values.

_PUBLIC_IP_CMDS = {
    # IMDSv2: session token then the public-ipv4 document.
    "aws": (
        'TOKEN="$(curl -fsS -m 10 -X PUT '
        '"http://169.254.169.254/latest/api/token" '
        "-H 'X-aws-ec2-metadata-token-ttl-seconds: 300')\" && "
        'curl -fsS -m 10 -H "X-aws-ec2-metadata-token: ${TOKEN}" '
        '"http://169.254.169.254/latest/meta-data/public-ipv4"'
    ),
    "gcp": (
        "curl -fsS -m 10 -H 'Metadata-Flavor: Google' "
        '"http://metadata.google.internal/computeMetadata/v1/instance/'
        'network-interfaces/0/access-configs/0/external-ip"'
    ),
}

STARTUP_TEMPLATE = """#!/usr/bin/env bash
# =============================================================================
# Homebox cloud-node bootstrap (rendered by app/nodeprovision.py)
# =============================================================================
# Boots a FULL homebox peer on a cloud VM and joins it to the cluster:
#   1. Install Docker (if the image doesn't already have it).
#   2. Pre-seed ~/.homebox/secrets.json with admin credentials WE generate, so
#      this script knows the plaintext password (the installer only persists a
#      bcrypt hash and reuses an existing one).
#   3. Run the Homebox installer non-interactively (no node-role override —
#      this is a full peer that serves apps).
#   4. Wait for the admin API on :7765, log in, and join the cluster with the
#      join token; peer_url is this VM's public IP.
# Idempotent: re-running converges. Logged to /var/log/homebox-node-bootstrap.log.
# =============================================================================
set -euo pipefail

# ── Rendered parameters ──────────────────────────────────────────────────────
JOIN_TOKEN=__JOIN_TOKEN__
CONTROL_PLANE_URL=__CONTROL_PLANE_URL__
INSTALL_URL=__INSTALL_URL__
NODE_NAME=__NODE_NAME__

ADMIN_PORT=7765
ADMIN_BASE="http://localhost:${ADMIN_PORT}"
SECRETS_DIR="/root/.homebox"
SECRETS_FILE="${SECRETS_DIR}/secrets.json"
ADMIN_USERNAME="homebox"

LOG=/var/log/homebox-node-bootstrap.log
exec > >(tee -a "$LOG") 2>&1
echo "=== homebox-node bootstrap $(date -u +%FT%TZ) node=${NODE_NAME} ==="

log()  { printf '[node] %s\\n' "$*"; }
fail() { printf '[node][FAIL] %s\\n' "$*" >&2; exit 1; }

public_ip() {
    __PUBLIC_IP_CMD__
}

# ── 1. Docker ────────────────────────────────────────────────────────────────
ensure_docker() {
    if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
        log "Docker present: $(docker --version)"
        return
    fi
    log "Installing Docker..."
    curl -fsSL https://get.docker.com | sh
    systemctl enable --now docker 2>/dev/null || true
    docker info >/dev/null 2>&1 || fail "Docker failed to start"
}

# ── 2. Pre-seed admin credentials (so we know the plaintext password) ─────────
seed_credentials() {
    mkdir -p "$SECRETS_DIR"
    chmod 700 "$SECRETS_DIR"

    if [ -f "${SECRETS_DIR}/node-admin-password" ]; then
        ADMIN_PASSWORD="$(cat "${SECRETS_DIR}/node-admin-password")"
        log "Reusing previously generated admin password"
    else
        ADMIN_PASSWORD="$(tr -dc 'A-Za-z0-9' </dev/urandom | head -c 32)"
        printf '%s' "$ADMIN_PASSWORD" > "${SECRETS_DIR}/node-admin-password"
        chmod 600 "${SECRETS_DIR}/node-admin-password"
    fi

    # Only seed secrets.json if the installer hasn't produced one yet — the
    # installer reuses an existing .admin.password_hash, so our known password
    # stays valid.
    if [ ! -f "$SECRETS_FILE" ]; then
        log "Generating bcrypt hash for the admin password"
        local hash
        hash="$(docker run --rm httpd:2-alpine htpasswd -nbB \\
            "$ADMIN_USERNAME" "$ADMIN_PASSWORD" 2>/dev/null \\
            | sed -n "s/^${ADMIN_USERNAME}://p")"
        [ -n "$hash" ] || fail "Failed to generate bcrypt hash"
        cat > "$SECRETS_FILE" <<JSON
{
  "admin": {
    "username": "${ADMIN_USERNAME}",
    "password_hash": "${hash}"
  },
  "identities": []
}
JSON
        chmod 600 "$SECRETS_FILE"
        log "Seeded ${SECRETS_FILE}"
    else
        log "secrets.json already exists — leaving it (installer will reuse it)"
    fi
}

# ── 3. Install Homebox (full peer — deliberately no node-role export) ────────
install_homebox() {
    export HOMEBOX_SUPPRESS_BANNER=1
    if docker ps --format '{{.Names}}' | grep -q '^homebox-admin$'; then
        log "Admin container already running — skipping installer"
    else
        log "Running Homebox installer"
        curl -fsSL "$INSTALL_URL" | bash
    fi
}

# ── 4. Wait for the admin API, log in, join the cluster ──────────────────────
wait_for_admin() {
    log "Waiting for admin API on :${ADMIN_PORT}"
    for _ in $(seq 1 120); do
        if curl -fsS -m 5 "${ADMIN_BASE}/api/auth/me" >/dev/null 2>&1 \\
           || curl -fsS -m 5 -o /dev/null "${ADMIN_BASE}/login" 2>/dev/null; then
            log "Admin API is up"
            return 0
        fi
        sleep 5
    done
    fail "Admin API did not come up on :${ADMIN_PORT}"
}

COOKIE_JAR="$(mktemp)"

login() {
    log "Logging into the admin API"
    curl -fsS -m 20 -c "$COOKIE_JAR" -X POST "${ADMIN_BASE}/api/auth/login" \\
        -H 'Content-Type: application/json' \\
        -d "$(printf '{"username": "%s", "password": "%s"}' "$ADMIN_USERNAME" "$ADMIN_PASSWORD")" \\
        >/dev/null || fail "Login failed"
}

join_cluster() {
    local ip peer_url
    ip="$(public_ip)" || fail "Could not read the VM's public IP"
    peer_url="http://${ip}"
    log "Joining cluster as ${NODE_NAME} (peer_url=${peer_url})"

    # Already in a cluster? /api/cluster reports active=true. Idempotent re-run.
    local status
    status="$(curl -fsS -m 20 -b "$COOKIE_JAR" "${ADMIN_BASE}/api/cluster" 2>/dev/null || echo '{}')"
    if printf '%s' "$status" | grep -q '"active":[[:space:]]*true'; then
        log "Node already in a cluster — skipping join"
        return 0
    fi

    local body
    body="$(cat <<JSON
{"join_token": "${JOIN_TOKEN}", "peer_url": "${peer_url}", "node_name": "${NODE_NAME}", "control_plane_url": "${CONTROL_PLANE_URL}"}
JSON
)"
    curl -fsS -m 60 -b "$COOKIE_JAR" -c "$COOKIE_JAR" -X POST "${ADMIN_BASE}/api/cluster/join" \\
        -H 'Content-Type: application/json' -d "$body" >/dev/null \\
        || fail "Cluster join request failed"

    # The admin restarts (~2s) to adopt the cluster keys — wait for it out of
    # politeness; the cluster roster is the source of truth for join success.
    log "Join accepted; waiting for admin to restart and adopt cluster keys"
    sleep 8
    wait_for_admin
}

main() {
    ensure_docker
    seed_credentials
    install_homebox
    wait_for_admin
    login
    join_cluster
    log "=== bootstrap complete ==="
}

main "$@"
"""


def render_startup_script(
    *,
    provider: str,
    join_token: str,
    control_plane_url: str,
    node_name: str,
    install_url: str | None = None,
) -> str:
    """Deterministic render of the node bootstrap script. Values are
    shlex-quoted into the parameter block; the public-IP probe is
    provider-specific (IMDSv2 vs the GCE metadata server)."""
    if provider not in _PUBLIC_IP_CMDS:
        raise NodeProvisionError(f"unsupported provider {provider!r}")
    out = STARTUP_TEMPLATE
    for marker, value in (
        ("__JOIN_TOKEN__", join_token),
        ("__CONTROL_PLANE_URL__", control_plane_url),
        ("__INSTALL_URL__", install_url
         or f"{settings.homebox_site_url.rstrip('/')}/install.sh"),
        ("__NODE_NAME__", node_name),
    ):
        out = out.replace(marker, shlex.quote(value))
    return out.replace("__PUBLIC_IP_CMD__", _PUBLIC_IP_CMDS[provider])


# ───── settings-table state ───────────────────────────────────────────────────


async def list_provisions(session: AsyncSession) -> list[dict[str, Any]]:
    val = await clusterlib._get_setting(session, PROVISIONS_KEY)
    return [dict(e) for e in val] if isinstance(val, list) else []


async def _save_provisions(
    session: AsyncSession, entries: list[dict[str, Any]]
) -> None:
    await clusterlib._set_setting(session, PROVISIONS_KEY, entries)
    await session.commit()


# ───── cloud plumbing (mirrors targets/aws_ec2_db.py / gcp_gce_db.py) ─────────


def _aws_client(integ: Integration, region: str) -> AwsClient:
    secret = crypto.decrypt(integ.secret_encrypted or "")
    key_id, _, key_secret = secret.partition(":")
    return AwsClient(key_id, key_secret, region, transport=_TRANSPORT)


def _gcp_client(integ: Integration) -> GcpClient:
    secret = crypto.decrypt(integ.secret_encrypted or "")
    try:
        sa = json.loads(secret)
    except ValueError:
        raise NodeProvisionError(
            "The GCP integration's service-account key is unreadable — "
            "reconnect the account in Integrations.", status=502)
    return GcpClient(sa, transport=_TRANSPORT)


def _gce_zone(region: str) -> str:
    """Accept either a region ("us-central1") or a full zone ("us-central1-a")."""
    return region if re.search(r"-[a-z]$", region) else f"{region}-a"


def _gce_instance_name(name: str) -> str:
    """RFC1035-sanitized deterministic GCE instance name."""
    out = re.sub(r"-{2,}", "-", re.sub(r"[^a-z0-9-]+", "-",
                                       f"homebox-node-{name}".lower())).strip("-")
    if not out or not out[0].isalpha():
        out = "n-" + out
    return out[:63].rstrip("-")


def _instance_items(root) -> list:
    return [
        el for el in root.iter()
        if _local(el.tag) == "item"
        and any(_local(c.tag) == "instanceId" for c in el)
    ]


def _ec2_instance_state(inst) -> str:
    for el in inst.iter():
        if _local(el.tag) == "instanceState":
            for child in el:
                if _local(child.tag) == "name":
                    return (child.text or "").strip()
    return ""


async def _aws_resolve_ami(aws: AwsClient) -> str:
    """Newest Ubuntu 24.04 LTS AMI in the client's region (AMI ids are
    region-specific, so they can't be hardcoded like GCE's image family)."""
    root = await aws.ec2("DescribeImages", {
        "Owner.1": _UBUNTU_OWNER,
        "Filter.1.Name": "name",
        "Filter.1.Value.1": _UBUNTU_NAME_FILTER,
        "Filter.2.Name": "state",
        "Filter.2.Value.1": "available",
    })
    images: list[tuple[str, str]] = []
    for el in root.iter():
        if _local(el.tag) != "item":
            continue
        image_id = creation = ""
        for child in el:
            if _local(child.tag) == "imageId":
                image_id = (child.text or "").strip()
            elif _local(child.tag) == "creationDate":
                creation = (child.text or "").strip()
        if image_id:
            images.append((creation, image_id))
    if not images:
        raise NodeProvisionError(
            "No Ubuntu 24.04 AMI found in this region — pick another region.",
            status=502)
    return max(images)[1]


async def _aws_find_tagged_instance(aws: AwsClient, resource_name: str):
    """The live (pending|running) instance carrying our deterministic tag, or
    None — same adopt-don't-duplicate lookup as aws_ec2_db."""
    root = await aws.ec2("DescribeInstances", {
        "Filter.1.Name": f"tag:{_TAG_KEY}",
        "Filter.1.Value.1": resource_name,
        "Filter.2.Name": "instance-state-name",
        "Filter.2.Value.1": "pending",
        "Filter.2.Value.2": "running",
    })
    items = _instance_items(root)
    return items[0] if items else None


async def _aws_ensure_security_group(aws: AwsClient, resource_name: str) -> str:
    sg_name = resource_name
    try:
        root = await aws.ec2("CreateSecurityGroup", {
            "GroupName": sg_name,
            "GroupDescription": (
                f"homebox node {resource_name}: peer API + WireGuard mesh"),
        })
        sg_id = _find_text(root, "groupId") or ""
    except AwsError as e:
        if e.code != "InvalidGroup.Duplicate":
            raise
        root = await aws.ec2("DescribeSecurityGroups", {
            "Filter.1.Name": "group-name",
            "Filter.1.Value.1": sg_name,
        })
        sg_id = _find_text(root, "groupId") or ""
    if not sg_id:
        raise NodeProvisionError(
            f"could not create or locate the security group {sg_name}.",
            status=502)
    # Peer API (http/https) + WireGuard. WireGuard authenticates by key and
    # the peer API by cluster-secret HMAC, so world-open like the db VMs.
    for proto, port in (("tcp", 80), ("tcp", 443), ("udp", 51820)):
        try:
            await aws.ec2("AuthorizeSecurityGroupIngress", {
                "GroupId": sg_id,
                "IpPermissions.1.IpProtocol": proto,
                "IpPermissions.1.FromPort": str(port),
                "IpPermissions.1.ToPort": str(port),
                "IpPermissions.1.IpRanges.1.CidrIp": "0.0.0.0/0",
            })
        except AwsError as e:
            if e.code != "InvalidPermission.Duplicate":
                raise
    return sg_id


async def _aws_create_node(
    integ: Integration, *, resource_name: str, region: str,
    machine: str, user_data: str,
) -> dict[str, Any]:
    """Create (or adopt) the EC2 instance; returns the resource dict."""
    import base64
    aws = _aws_client(integ, region)
    existing = await _aws_find_tagged_instance(aws, resource_name)
    if existing is not None:
        instance_id = _find_text(existing, "instanceId") or ""
        log.info("adopting existing EC2 instance %s for %s",
                 instance_id, resource_name)
        return {"instance_id": instance_id, "region": region}
    ami = await _aws_resolve_ami(aws)
    sg_id = await _aws_ensure_security_group(aws, resource_name)
    root = await aws.ec2("RunInstances", {
        "ImageId": ami,
        "InstanceType": machine,
        "MinCount": "1",
        "MaxCount": "1",
        "UserData": base64.b64encode(user_data.encode()).decode(),
        "SecurityGroupId.1": sg_id,
        "TagSpecification.1.ResourceType": "instance",
        "TagSpecification.1.Tag.1.Key": _TAG_KEY,
        "TagSpecification.1.Tag.1.Value": resource_name,
        "TagSpecification.1.Tag.2.Key": "Name",
        "TagSpecification.1.Tag.2.Value": resource_name,
    })
    items = _instance_items(root)
    instance_id = _find_text(items[0], "instanceId") if items else None
    if not instance_id:
        raise NodeProvisionError(
            "EC2 RunInstances returned no instance id.", status=502)
    return {"instance_id": instance_id, "sg_id": sg_id, "region": region}


async def _gcp_ensure_firewall(gcp: GcpClient) -> None:
    body = {
        "name": _GCE_FIREWALL_NAME,
        "network": "global/networks/default",
        "direction": "INGRESS",
        "sourceRanges": ["0.0.0.0/0"],
        "targetTags": [_GCE_NETWORK_TAG],
        "allowed": [
            {"IPProtocol": "tcp", "ports": ["80", "443"]},
            {"IPProtocol": "udp", "ports": ["51820"]},
        ],
    }
    try:
        await gcp.compute("POST", "global/firewalls", json=body)
    except GcpError as e:
        if e.status != 409:  # already exists — first writer wins
            raise


async def _gcp_create_node(
    integ: Integration, *, resource_name: str, region: str,
    machine: str, user_data: str,
) -> dict[str, Any]:
    """Create (or adopt) the GCE instance; returns the resource dict."""
    gcp = _gcp_client(integ)
    zone = _gce_zone(region)
    name = _gce_instance_name(resource_name.removeprefix("homebox-node-"))
    try:
        await gcp.compute("GET", f"zones/{zone}/instances/{name}")
        log.info("adopting existing GCE instance %s for %s", name, resource_name)
        return {"instance_name": name, "zone": zone, "project": gcp.project_id}
    except GcpError as e:
        if e.status != 404:
            raise
    await _gcp_ensure_firewall(gcp)
    body = {
        "name": name,
        "machineType": f"zones/{zone}/machineTypes/{machine}",
        "disks": [{
            "boot": True,
            "autoDelete": True,
            "initializeParams": {"sourceImage": _GCE_BOOT_IMAGE},
        }],
        "networkInterfaces": [{
            "network": "global/networks/default",
            "accessConfigs": [{
                "type": "ONE_TO_ONE_NAT",
                "name": "External NAT",
            }],
        }],
        # GCE runs the `startup-script` metadata value as root on boot —
        # same delivery the cloud-mirror provisioner uses.
        "metadata": {"items": [{"key": "startup-script", "value": user_data}]},
        "tags": {"items": [_GCE_NETWORK_TAG]},
        "labels": {_TAG_KEY: name},
    }
    await gcp.compute("POST", f"zones/{zone}/instances", json=body)
    return {"instance_name": name, "zone": zone, "project": gcp.project_id}


# ───── public API ─────────────────────────────────────────────────────────────


async def provision_node(
    session: AsyncSession,
    *,
    name: str,
    provider: str,
    integration_id: int,
    region: str,
    machine: str | None = None,
) -> dict[str, Any]:
    """Provision a full Homebox node VM that auto-joins the current cluster.
    Returns the (persisted) provision entry. Idempotent per name: an existing
    non-error entry with the same name is returned as-is."""
    name = (name or "").strip()
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,40}", name):
        raise NodeProvisionError(
            "name must be lowercase letters, digits and dashes "
            "(1-41 chars, starting with a letter or digit).")
    if provider not in ("aws", "gcp"):
        raise NodeProvisionError("provider must be 'aws' or 'gcp'.")
    region = (region or "").strip()
    if not region:
        raise NodeProvisionError("region is required.")

    acct = await clusterlib.load_account(session)
    if not acct:
        raise NodeProvisionError(
            "Link a homebox.sh account first — cloud nodes are provisioned "
            "onto your linked cloud account.", status=412)
    state = await clusterlib.load_cluster(session)
    if not state:
        raise NodeProvisionError(
            "This node is not part of a cluster — the new VM needs a cluster "
            "to join. Create one first.", status=412)

    integ = await session.get(Integration, integration_id)
    if integ is None:
        raise NodeProvisionError("Integration not found.", status=404)
    if integ.provider != provider:
        raise NodeProvisionError(
            f"Integration {integration_id} is a {integ.provider!r} "
            f"connection, not {provider!r}.")

    # Idempotent re-provision: same name + still alive → hand back the entry.
    entries = await list_provisions(session)
    for entry in entries:
        if entry.get("name") == name and entry.get("status") != "error":
            return entry

    # Mint the join token — the same clusterlib call POST /join-token makes.
    acct_token = clusterlib.account_token(state)
    if not acct_token:
        raise NodeProvisionError(
            "No account token on this node — provision from the founding "
            "node.", status=412)
    minted = await clusterlib._cp(
        "POST", state["control_plane_url"],
        f"/v1/clusters/{state['cluster_id']}/join-tokens",
        token=acct_token,
    )
    join_token = str(minted.get("join_token") or "")
    if not join_token:
        raise NodeProvisionError(
            "The control plane returned no join token.", status=502)

    resource_name = f"homebox-node-{name}"
    script = render_startup_script(
        provider=provider,
        join_token=join_token,
        control_plane_url=state["control_plane_url"],
        node_name=name,
    )
    machine = machine or (
        _AWS_DEFAULT_MACHINE if provider == "aws" else _GCP_DEFAULT_MACHINE)

    try:
        if provider == "aws":
            resource = await _aws_create_node(
                integ, resource_name=resource_name, region=region,
                machine=machine, user_data=script)
        else:
            resource = await _gcp_create_node(
                integ, resource_name=resource_name, region=region,
                machine=machine, user_data=script)
    except (AwsError, GcpError) as e:
        raise NodeProvisionError(f"cloud VM create failed: {e}", status=502) from e
    except httpx.HTTPError as e:
        raise NodeProvisionError(f"cloud VM create failed: {e}", status=502) from e

    entry = {
        "id": _secrets.token_urlsafe(8),
        "name": name,
        "provider": provider,
        "integration_id": integration_id,
        "region": region,
        "machine": machine,
        "status": "booting",
        "created_at": datetime.utcnow().isoformat(),
        # Snapshot so refresh can tell a NEW node from a same-named old one.
        "roster_before": [
            n.get("node_id") for n in (state.get("roster") or [])
            if n.get("node_id")
        ],
        "resource": resource,
    }
    entries = await list_provisions(session)  # re-read: _cp may have slept
    entries.append(entry)
    await _save_provisions(session, entries)
    return entry


def _find_joined_node(
    state: dict[str, Any] | None, entry: dict[str, Any]
) -> str | None:
    """The node_id of a NEW roster node whose name matches the provision."""
    if not state:
        return None
    before = set(entry.get("roster_before") or [])
    for n in state.get("roster") or []:
        nid = n.get("node_id")
        if nid and nid not in before and (n.get("name") or "") == entry["name"]:
            return str(nid)
    return None


async def _instance_dead(entry: dict[str, Any], integ: Integration | None) -> str | None:
    """A terminal error message when the VM is gone/dead, else None. Transient
    cloud API failures are swallowed (checked again on the next refresh)."""
    if integ is None:
        return None  # integration deleted mid-provision; only the timeout fires
    resource = entry.get("resource") or {}
    try:
        if entry["provider"] == "aws":
            instance_id = resource.get("instance_id")
            if not instance_id:
                return "no instance was recorded for this provision."
            aws = _aws_client(integ, str(resource.get("region")
                                         or entry.get("region") or "us-east-1"))
            root = await aws.ec2(
                "DescribeInstances", {"InstanceId.1": str(instance_id)})
            items = _instance_items(root)
            if not items:
                return f"EC2 instance {instance_id} no longer exists."
            ec2_state = _ec2_instance_state(items[0])
            if ec2_state in _EC2_DEAD_STATES:
                return f"EC2 instance {instance_id} ended in state {ec2_state}."
        else:
            instance_name = resource.get("instance_name")
            if not instance_name:
                return "no instance was recorded for this provision."
            gcp = _gcp_client(integ)
            zone = resource.get("zone") or _gce_zone(entry.get("region") or "")
            try:
                r = await gcp.compute(
                    "GET", f"zones/{zone}/instances/{instance_name}")
            except GcpError as e:
                if e.status == 404:
                    return f"GCE instance {instance_name} no longer exists."
                raise
            status = str((r.json() or {}).get("status") or "")
            if status in _GCE_DEAD_STATUSES:
                return f"GCE instance {instance_name} ended in status {status}."
    except (AwsError, GcpError, httpx.HTTPError, NodeProvisionError) as e:
        log.info("provision %s instance check failed (transient): %s",
                 entry.get("id"), e)
    return None


async def refresh_provisions(session: AsyncSession) -> list[dict[str, Any]]:
    """Advance every non-terminal provision: roster join detection, cloud
    instance liveness, 30-minute timeout. Called lazily from the GET route —
    there is deliberately no background loop."""
    entries = await list_provisions(session)
    if not any(e.get("status") in ("creating", "booting") for e in entries):
        return entries
    state = await clusterlib.load_cluster(session)
    changed = False
    for entry in entries:
        if entry.get("status") not in ("creating", "booting"):
            continue
        node_id = _find_joined_node(state, entry)
        if node_id:
            entry["status"] = "joined"
            entry["node_id"] = node_id
            changed = True
            continue
        integ = await session.get(Integration, entry.get("integration_id"))
        dead = await _instance_dead(entry, integ)
        if dead:
            entry["status"] = "error"
            entry["error"] = dead
            changed = True
            continue
        try:
            created = datetime.fromisoformat(entry.get("created_at") or "")
        except ValueError:
            created = datetime.utcnow()
        if datetime.utcnow() - created > PROVISION_TIMEOUT:
            entry["status"] = "error"
            entry["error"] = (
                f"node did not join within "
                f"{int(PROVISION_TIMEOUT.total_seconds() // 60)} minutes — "
                "check the VM's /var/log/homebox-node-bootstrap.log.")
            changed = True
    if changed:
        await _save_provisions(session, entries)
    return entries


async def teardown_provision(session: AsyncSession, provision_id: str) -> dict[str, Any]:
    """Delete the provision's VM (idempotent, like the db_vm destroy paths)
    and remove the entry. A joined node is NOT auto-evicted — the user evicts
    it via the cluster roster."""
    entries = await list_provisions(session)
    entry = next((e for e in entries if e.get("id") == provision_id), None)
    if entry is None:
        raise NodeProvisionError("Provision not found.", status=404)

    integ = await session.get(Integration, entry.get("integration_id"))
    resource = entry.get("resource") or {}
    if integ is not None:
        try:
            if entry["provider"] == "aws":
                aws = _aws_client(
                    integ, str(resource.get("region")
                               or entry.get("region") or "us-east-1"))
                instance_id = resource.get("instance_id")
                if instance_id:
                    try:
                        await aws.ec2("TerminateInstances",
                                      {"InstanceId.1": str(instance_id)})
                    except AwsError as e:
                        if not (e.code or "").startswith("InvalidInstanceID"):
                            raise
                sg_id = resource.get("sg_id")
                if sg_id:
                    # Best-effort: stays referenced until ENIs detach.
                    try:
                        await aws.ec2("DeleteSecurityGroup",
                                      {"GroupId": str(sg_id)})
                    except AwsError as e:
                        log.info("leaving security group %s behind: %s", sg_id, e)
            else:
                instance_name = resource.get("instance_name")
                if instance_name:
                    gcp = _gcp_client(integ)
                    zone = (resource.get("zone")
                            or _gce_zone(entry.get("region") or ""))
                    try:
                        await gcp.compute(
                            "DELETE", f"zones/{zone}/instances/{instance_name}")
                    except GcpError as e:
                        if e.status != 404:
                            raise
        except (AwsError, GcpError) as e:
            raise NodeProvisionError(
                f"cloud VM delete failed: {e}", status=502) from e
        except httpx.HTTPError as e:
            raise NodeProvisionError(
                f"cloud VM delete failed: {e}", status=502) from e

    entries = [e for e in entries if e.get("id") != provision_id]
    await _save_provisions(session, entries)
    return entry
