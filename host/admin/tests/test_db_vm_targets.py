"""Tests for the cloud database-VM targets (app/targets/db_vm_common.py,
aws_ec2_db.py, gcp_gce_db.py).

The shared cloud-init renderer is golden-asserted (wg0.conf, the pgEdge
init-script semantics mirrored from cluster_db._INIT_SCRIPTS, the docker
run). Both providers run against httpx.MockTransport with routed fakes —
EC2's Query protocol with XML responses, GCE's compute/v1 JSON with zone
operations — with the poll intervals zeroed. No network, no credentials.
"""
from __future__ import annotations

import asyncio
import base64
import json
import sys
import urllib.parse
from pathlib import Path

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app.targets.aws_ec2_db as ec2_mod  # noqa: E402
import app.targets.gcp_gce_db as gce_mod  # noqa: E402
from app.targets.aws_ec2_db import Ec2DbTarget  # noqa: E402
from app.targets.base import TargetDeployCtx, TargetError  # noqa: E402
from app.targets.db_vm_common import (  # noqa: E402
    DbVmSpec,
    mesh_ip_for,
    render_cloud_init,
    spec_from_config,
)
from app.targets.gcp_gce_db import GceDbTarget, _instance_name  # noqa: E402

ORDINAL = 0xF000  # 61440 → mesh ip 10.77.240.0, spock node n61440
PEERS = [
    {"public_key": "PEERKEY1=", "allowed_ips": "10.77.0.1/32"},
    {"public_key": "PEERKEY2=", "allowed_ips": "10.77.0.2/32"},
]
PG_IMAGE = "ghcr.io/pgedge/pgedge-postgres:16-spock5-standard"

BASE_CONFIG = {
    "mesh_ordinal": ORDINAL,
    "wg_private_key": "VMPRIVKEY=",
    "wg_public_key": "VMPUBKEY=",
    "wg_peers": PEERS,
    "db": {
        "db_name": "app", "admin_user": "app", "admin_password": "adm-pw",
        "repl_user": "pgedge", "repl_password": "repl-pw",
    },
}


def run(coro):
    return asyncio.run(coro)


def make_spec(**over) -> DbVmSpec:
    kw = dict(
        ordinal=ORDINAL, mesh_ip=mesh_ip_for(ORDINAL),
        wg_private_key="VMPRIVKEY=", wg_peers=[dict(p) for p in PEERS],
        pg_image=PG_IMAGE,
        db_name="app", admin_user="app", admin_password="adm-pw",
        repl_user="pgedge", repl_password="repl-pw",
        node_name=f"n{ORDINAL}", open_pg_public=False,
    )
    kw.update(over)
    return DbVmSpec(**kw)


def make_ctx(config: dict):
    lines: list[str] = []

    async def log(line: str) -> None:
        lines.append(line)

    ctx = TargetDeployCtx(
        project_name="listless", env_name="dev", service_name="db",
        kind="database", rd=Path("/nonexistent"), hostname=None,
        config=config, log=log,
    )
    return ctx, lines  # ctx.resource_name == homebox-listless-dev-db


@pytest.fixture(autouse=True)
def fast_poll(monkeypatch):
    monkeypatch.setattr(ec2_mod, "_POLL_INTERVAL", 0.0)
    monkeypatch.setattr(gce_mod, "OP_POLL_INTERVAL", 0)


# ═════ db_vm_common ═══════════════════════════════════════════════════════════


def test_mesh_ip_for_mirrors_meshlib():
    assert mesh_ip_for(0xF000) == "10.77.240.0"
    assert mesh_ip_for(0xF001) == "10.77.240.1"
    assert mesh_ip_for(0xF1FF) == "10.77.241.255"


def test_cloud_init_wireguard_config():
    out = render_cloud_init(make_spec())
    assert out.startswith("#!/bin/bash")
    assert "/etc/wireguard/wg0.conf" in out
    assert "Address = 10.77.240.0/16" in out
    assert "ListenPort = 51820" in out
    assert "PrivateKey = VMPRIVKEY=" in out
    assert out.count("[Peer]") == 2
    assert "PublicKey = PEERKEY1=" in out
    assert "PublicKey = PEERKEY2=" in out
    assert "AllowedIPs = 10.77.0.1/32" in out
    assert "AllowedIPs = 10.77.0.2/32" in out
    assert out.count("PersistentKeepalive = 25") == 2
    # Homebox nodes dial the VM — the VM's peers must NOT pin addresses.
    assert "Endpoint" not in out
    assert "systemctl enable wg-quick@wg0" in out
    assert "curl -fsSL https://get.docker.com | sh" in out


def test_cloud_init_pg_gucs_mirror_cluster_db():
    out = render_cloud_init(make_spec())
    assert 'LIBS="pg_stat_statements,snowflake,spock"' in out
    assert "wal_level = 'logical'" in out
    assert "max_replication_slots = 16" in out
    assert "max_wal_senders = 16" in out
    assert "track_commit_timestamp = 'on'" in out
    assert "spock.conflict_resolution = 'last_update_wins'" in out
    assert "spock.save_resolutions = 'on'" in out
    assert "spock.enable_ddl_replication = 'off'" in out
    # Ordinal baked in; lolor.node lands in postgresql.conf (not ALTER SYSTEM).
    assert "snowflake.node = '61440'" in out
    assert "snowflake.node_id = '61440'" in out
    assert "lolor.node = '61440'" in out
    # lolor.node must land in postgresql.conf, never via ALTER SYSTEM (the
    # comment in the script explains why) — no such statement is issued.
    assert "ALTER SYSTEM SET" not in out


def test_cloud_init_extensions_hba_and_spock_node():
    out = render_cloud_init(make_spec())
    assert "for EXT in spock snowflake lolor; do" in out
    assert "CREATE EXTENSION IF NOT EXISTS" in out
    assert 'host all all 0.0.0.0/0 md5' in out
    assert "CREATE ROLE" in out and "LOGIN REPLICATION SUPERUSER" in out
    assert "spock.node_create(node_name := 'n61440'" in out
    assert "dsn := 'host=localhost port=5432" in out


def test_cloud_init_docker_run():
    out = render_cloud_init(make_spec())
    assert "--restart unless-stopped" in out
    assert "--name homebox-db" in out
    assert "-p 5432:5432" in out
    assert "-v pgdata:/var/lib/pgsql" in out
    assert "-v /opt/homebox-db/init:/docker-entrypoint-initdb.d" in out
    assert "-e POSTGRES_USER=app" in out
    assert "-e POSTGRES_PASSWORD=adm-pw" in out
    assert "-e POSTGRES_DB=app" in out
    assert "-e PGEDGE_USER=pgedge" in out
    assert "-e PGEDGE_PASSWORD=repl-pw" in out
    assert PG_IMAGE in out
    assert "touch /var/lib/homebox-db-ready" in out


def test_cloud_init_deterministic():
    assert render_cloud_init(make_spec()) == render_cloud_init(make_spec())


def test_spec_from_config_full_and_defaults():
    spec = spec_from_config(dict(BASE_CONFIG))
    assert spec.ordinal == ORDINAL
    assert spec.mesh_ip == "10.77.240.0"
    assert spec.node_name == "n61440"
    assert spec.pg_image == PG_IMAGE  # PGEDGE_IMAGE with the default major
    assert spec.open_pg_public is False
    assert spec.wg_peers == PEERS

    cfg = dict(BASE_CONFIG, pg_major="17", open_pg_public=True,
               mesh_ip="10.77.240.9")
    spec = spec_from_config(cfg)
    assert spec.pg_image == "ghcr.io/pgedge/pgedge-postgres:17-spock5-standard"
    assert spec.open_pg_public is True
    assert spec.mesh_ip == "10.77.240.9"


def test_spec_from_config_missing_pieces_raise():
    cfg = {k: v for k, v in BASE_CONFIG.items() if k != "mesh_ordinal"}
    with pytest.raises(TargetError) as exc:
        spec_from_config(cfg)
    assert "mesh_ordinal" in str(exc.value)

    cfg = dict(BASE_CONFIG, wg_private_key="")
    with pytest.raises(TargetError):
        spec_from_config(cfg)

    cfg = dict(BASE_CONFIG, db={"admin_password": "x"})  # no repl_password
    with pytest.raises(TargetError):
        spec_from_config(cfg)


# ═════ EC2 ════════════════════════════════════════════════════════════════════

IID = "i-0abc12345"
SG_ID = "sg-0123"
PUBLIC_IP = "3.4.5.6"
EC2_NAME = "homebox-listless-dev-db"
EC2_CONFIG = dict(BASE_CONFIG, ami="ami-123")

STS_XML = (
    b'<GetCallerIdentityResponse xmlns="https://sts.amazonaws.com/doc/2011-06-15/">'
    b"<GetCallerIdentityResult><Arn>arn:aws:iam::123456789012:user/homebox</Arn>"
    b"<UserId>AIDAEXAMPLE</UserId><Account>123456789012</Account>"
    b"</GetCallerIdentityResult></GetCallerIdentityResponse>"
)


def ec2_error(code: str, message: str = "boom", status: int = 400) -> httpx.Response:
    return httpx.Response(status, content=(
        f"<Response><Errors><Error><Code>{code}</Code>"
        f"<Message>{message}</Message></Error></Errors>"
        f"<RequestID>req-1</RequestID></Response>"
    ).encode())


def _instances_xml(instances: list[dict]) -> bytes:
    items = []
    for inst in instances:
        ip = f"<ipAddress>{inst['ip']}</ipAddress>" if inst.get("ip") else ""
        items.append(
            "<item>"
            f"<instanceId>{inst['id']}</instanceId>"
            f"<instanceState><code>0</code><name>{inst['state']}</name></instanceState>"
            f"<groupSet><item><groupId>{inst.get('sg', SG_ID)}</groupId>"
            "<groupName>homebox-db</groupName></item></groupSet>"
            f"{ip}"
            "</item>"
        )
    inner = "".join(items)
    body = (
        '<DescribeInstancesResponse xmlns="http://ec2.amazonaws.com/doc/2016-11-15/">'
        f"<reservationSet><item><instancesSet>{inner}</instancesSet></item>"
        "</reservationSet></DescribeInstancesResponse>"
    ) if items else (
        "<DescribeInstancesResponse><reservationSet/></DescribeInstancesResponse>"
    )
    return body.encode()


class FakeEc2:
    """Routes MockTransport requests like the EC2 Query API (+ STS)."""

    def __init__(self, *, existing: dict | None = None,
                 polls: list[dict] | None = None,
                 sg_duplicate: bool = False, auth_duplicate: bool = False,
                 terminate_response: httpx.Response | None = None,
                 delete_sg_response: httpx.Response | None = None,
                 sts_response: httpx.Response | None = None):
        self.existing = existing  # the tag-filter DescribeInstances result
        # by-id DescribeInstances results, front-to-back holding the last.
        self.polls = list(polls or [
            {"id": IID, "state": "pending", "ip": None},
            {"id": IID, "state": "running", "ip": PUBLIC_IP},
        ])
        self.sg_duplicate = sg_duplicate
        self.auth_duplicate = auth_duplicate
        self.terminate_response = terminate_response
        self.delete_sg_response = delete_sg_response
        self.sts_response = sts_response
        self.calls: list[tuple[str, dict]] = []

    def actions(self) -> list[str]:
        return [a for a, _ in self.calls]

    def payload(self, action: str) -> dict:
        return next(f for a, f in self.calls if a == action)

    def payloads(self, action: str) -> list[dict]:
        return [f for a, f in self.calls if a == action]

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.host == "sts.amazonaws.com":
            self.calls.append(("GetCallerIdentity", {}))
            return self.sts_response or httpx.Response(200, content=STS_XML)
        assert request.url.host == "ec2.us-east-2.amazonaws.com", request.url.host
        form = dict(urllib.parse.parse_qsl(request.content.decode()))
        action = form["Action"]
        self.calls.append((action, form))
        if action == "DescribeInstances":
            if "InstanceId.1" in form:
                inst = self.polls[0]
                if len(self.polls) > 1:
                    self.polls.pop(0)
                return httpx.Response(
                    200, content=_instances_xml([inst] if inst else []))
            found = [self.existing] if self.existing else []
            return httpx.Response(200, content=_instances_xml(found))
        if action == "CreateSecurityGroup":
            if self.sg_duplicate:
                return ec2_error("InvalidGroup.Duplicate", "already exists")
            return httpx.Response(200, content=(
                "<CreateSecurityGroupResponse><return>true</return>"
                f"<groupId>{SG_ID}</groupId></CreateSecurityGroupResponse>"
            ).encode())
        if action == "DescribeSecurityGroups":
            return httpx.Response(200, content=(
                "<DescribeSecurityGroupsResponse><securityGroupInfo><item>"
                f"<groupId>{SG_ID}</groupId>"
                f"<groupName>{form.get('Filter.1.Value.1', '')}</groupName>"
                "</item></securityGroupInfo></DescribeSecurityGroupsResponse>"
            ).encode())
        if action == "AuthorizeSecurityGroupIngress":
            if self.auth_duplicate:
                return ec2_error("InvalidPermission.Duplicate", "rule exists")
            return httpx.Response(200, content=(
                b"<AuthorizeSecurityGroupIngressResponse><return>true</return>"
                b"</AuthorizeSecurityGroupIngressResponse>"))
        if action == "RunInstances":
            return httpx.Response(200, content=(
                "<RunInstancesResponse><reservationId>r-1</reservationId>"
                "<instancesSet><item>"
                f"<instanceId>{IID}</instanceId>"
                "<instanceState><code>0</code><name>pending</name></instanceState>"
                "</item></instancesSet></RunInstancesResponse>"
            ).encode())
        if action == "TerminateInstances":
            return self.terminate_response or httpx.Response(200, content=(
                b"<TerminateInstancesResponse><instancesSet/>"
                b"</TerminateInstancesResponse>"))
        if action == "DeleteSecurityGroup":
            return self.delete_sg_response or httpx.Response(200, content=(
                b"<DeleteSecurityGroupResponse><return>true</return>"
                b"</DeleteSecurityGroupResponse>"))
        raise AssertionError(f"unexpected EC2 action: {action}")


def ec2_target(fake: FakeEc2, config: dict | None = None,
               state: dict | None = None) -> Ec2DbTarget:
    return Ec2DbTarget(
        {"key_id": "AKID", "secret": "sk", "region": "us-east-2"},
        config if config is not None else dict(EC2_CONFIG),
        state or {},
        transport=httpx.MockTransport(fake.handler),
    )


def test_ec2_validate_ok_and_error():
    fake = FakeEc2()
    run(ec2_target(fake).validate())
    assert fake.actions() == ["GetCallerIdentity"]

    bad = FakeEc2(sts_response=httpx.Response(403, content=(
        b"<ErrorResponse><Error><Code>InvalidClientTokenId</Code>"
        b"<Message>The security token is invalid</Message></Error></ErrorResponse>"
    )))
    with pytest.raises(TargetError) as exc:
        run(ec2_target(bad).validate())
    assert "InvalidClientTokenId" in str(exc.value)


def test_ec2_fresh_deploy_full_flow():
    fake = FakeEc2()
    cfg = dict(EC2_CONFIG)
    ctx, lines = make_ctx(cfg)
    result = run(ec2_target(fake, cfg).deploy(ctx))
    actions = fake.actions()

    # Idempotency probe first, then SG, then launch.
    assert actions[0] == "DescribeInstances"
    sg = fake.payload("CreateSecurityGroup")
    assert sg["GroupName"] == f"homebox-db-{EC2_NAME}"

    # Only the WireGuard rule by default (no public 5432, no allowed_cidrs).
    auth = fake.payloads("AuthorizeSecurityGroupIngress")
    assert len(auth) == 1
    assert auth[0]["GroupId"] == SG_ID
    assert auth[0]["IpPermissions.1.IpProtocol"] == "udp"
    assert auth[0]["IpPermissions.1.FromPort"] == "51820"
    assert auth[0]["IpPermissions.1.ToPort"] == "51820"
    assert auth[0]["IpPermissions.1.IpRanges.1.CidrIp"] == "0.0.0.0/0"

    runp = fake.payload("RunInstances")
    assert runp["ImageId"] == "ami-123"
    assert runp["InstanceType"] == "t3.small"
    assert runp["MinCount"] == "1" and runp["MaxCount"] == "1"
    assert runp["SecurityGroupId.1"] == SG_ID
    assert runp["TagSpecification.1.ResourceType"] == "instance"
    assert runp["TagSpecification.1.Tag.1.Key"] == "homebox-db"
    assert runp["TagSpecification.1.Tag.1.Value"] == EC2_NAME
    assert runp["TagSpecification.1.Tag.2.Key"] == "Name"
    assert runp["TagSpecification.1.Tag.2.Value"] == EC2_NAME
    user_data = base64.b64decode(runp["UserData"]).decode()
    assert user_data == render_cloud_init(spec_from_config(cfg))
    assert "Address = 10.77.240.0/16" in user_data

    # Polled through pending to running + public IP.
    assert len(fake.payloads("DescribeInstances")) >= 3

    assert result.endpoint == PUBLIC_IP
    assert result.cname_target is None
    assert result.proxied is False
    assert result.state["instance_id"] == IID
    assert result.state["sg_id"] == SG_ID
    assert result.state["public_ip"] == PUBLIC_IP
    # Exact shapes targetslib.mesh_extra_peers / db_vm_extra_nodes read.
    assert result.state["mesh"] == {
        "ordinal": ORDINAL, "ip": "10.77.240.0", "wg_pubkey": "VMPUBKEY=",
        "endpoint": f"{PUBLIC_IP}:51820",
    }
    assert result.state["db"] == {"port": 5432, "node_name": "n61440"}
    assert any("launched EC2 instance" in ln for ln in lines)


def test_ec2_open_pg_public_adds_world_5432():
    fake = FakeEc2()
    cfg = dict(EC2_CONFIG, open_pg_public=True)
    ctx, _ = make_ctx(cfg)
    run(ec2_target(fake, cfg).deploy(ctx))
    auth = fake.payloads("AuthorizeSecurityGroupIngress")
    rules = {(f["IpPermissions.1.IpProtocol"], f["IpPermissions.1.FromPort"],
              f["IpPermissions.1.IpRanges.1.CidrIp"]) for f in auth}
    assert rules == {("udp", "51820", "0.0.0.0/0"),
                     ("tcp", "5432", "0.0.0.0/0")}


def test_ec2_allowed_cidrs_scope_5432():
    fake = FakeEc2()
    cfg = dict(EC2_CONFIG, allowed_cidrs=["10.0.0.0/8", "192.168.1.0/24"])
    ctx, _ = make_ctx(cfg)
    run(ec2_target(fake, cfg).deploy(ctx))
    auth = fake.payloads("AuthorizeSecurityGroupIngress")
    rules = {(f["IpPermissions.1.IpProtocol"], f["IpPermissions.1.FromPort"],
              f["IpPermissions.1.IpRanges.1.CidrIp"]) for f in auth}
    assert rules == {("udp", "51820", "0.0.0.0/0"),
                     ("tcp", "5432", "10.0.0.0/8"),
                     ("tcp", "5432", "192.168.1.0/24")}


def test_ec2_duplicate_sg_and_rules_tolerated():
    fake = FakeEc2(sg_duplicate=True, auth_duplicate=True)
    ctx, _ = make_ctx(dict(EC2_CONFIG))
    result = run(ec2_target(fake).deploy(ctx))
    # Duplicate group → looked up by name; duplicate rule → carried on.
    assert "DescribeSecurityGroups" in fake.actions()
    assert result.state["sg_id"] == SG_ID
    assert "RunInstances" in fake.actions()


def test_ec2_idempotent_second_deploy():
    fake = FakeEc2(
        existing={"id": IID, "state": "running", "ip": PUBLIC_IP},
        polls=[{"id": IID, "state": "running", "ip": PUBLIC_IP}],
    )
    ctx, _ = make_ctx(dict(EC2_CONFIG))
    result = run(ec2_target(fake).deploy(ctx))
    assert "RunInstances" not in fake.actions()
    assert "CreateSecurityGroup" not in fake.actions()
    assert result.endpoint == PUBLIC_IP
    assert result.state["instance_id"] == IID
    assert result.state["sg_id"] == SG_ID  # adopted from the instance's groupSet
    assert result.state["mesh"]["endpoint"] == f"{PUBLIC_IP}:51820"


def test_ec2_missing_ami_raises_before_any_mutation():
    fake = FakeEc2()
    cfg = {k: v for k, v in EC2_CONFIG.items() if k != "ami"}
    ctx, _ = make_ctx(cfg)
    with pytest.raises(TargetError) as exc:
        run(ec2_target(fake, cfg).deploy(ctx))
    assert "ami" in str(exc.value).lower()
    assert "CreateSecurityGroup" not in fake.actions()
    assert "RunInstances" not in fake.actions()


def test_ec2_destroy_terminates_and_deletes_sg():
    fake = FakeEc2()
    run(ec2_target(fake).destroy(
        {"instance_id": IID, "sg_id": SG_ID, "mesh": {"ordinal": ORDINAL}}))
    assert fake.payload("TerminateInstances")["InstanceId.1"] == IID
    assert fake.payload("DeleteSecurityGroup")["GroupId"] == SG_ID


def test_ec2_destroy_tolerates_gone_and_dependencies():
    gone = FakeEc2(terminate_response=ec2_error(
        "InvalidInstanceID.NotFound", "does not exist"))
    run(ec2_target(gone).destroy({"instance_id": IID, "sg_id": SG_ID}))  # no raise

    busy = FakeEc2(delete_sg_response=ec2_error(
        "DependencyViolation", "has a dependent object"))
    run(ec2_target(busy).destroy({"instance_id": IID, "sg_id": SG_ID}))  # no raise

    untouched = FakeEc2()
    run(ec2_target(untouched).destroy({}))
    run(ec2_target(untouched).destroy(None))
    assert untouched.calls == []


def test_ec2_destroy_raises_on_other_terminate_errors():
    fake = FakeEc2(terminate_response=ec2_error(
        "UnauthorizedOperation", "not allowed", status=403))
    with pytest.raises(TargetError):
        run(ec2_target(fake).destroy({"instance_id": IID}))


def test_ec2_probe_paths():
    up = FakeEc2(polls=[{"id": IID, "state": "running", "ip": PUBLIC_IP}])
    assert run(ec2_target(up).probe({"instance_id": IID})) is True

    pending = FakeEc2(polls=[{"id": IID, "state": "pending", "ip": None}])
    assert run(ec2_target(pending).probe({"instance_id": IID})) is False

    def not_found(request: httpx.Request) -> httpx.Response:
        return ec2_error("InvalidInstanceID.NotFound", "no such instance")

    t = Ec2DbTarget({"key_id": "k", "secret": "s", "region": "us-east-2"},
                    {}, {}, transport=httpx.MockTransport(not_found))
    assert run(t.probe({"instance_id": IID})) is False
    assert run(t.probe({})) is False


# ═════ GCE ════════════════════════════════════════════════════════════════════

GPROJECT = "proj-1"
ZONE = "us-central1-a"
GNAME = "homebox-listless-dev-db"
INSTANCES_PATH = f"/compute/v1/projects/{GPROJECT}/zones/{ZONE}/instances"
FIREWALLS_PATH = f"/compute/v1/projects/{GPROJECT}/global/firewalls"
OP_PATH = f"/compute/v1/projects/{GPROJECT}/zones/{ZONE}/operations/op-1"
NAT_IP = "35.1.2.3"


@pytest.fixture(scope="module")
def sa():
    """A fake parsed service-account key file with a real private key (the
    token exchange builds and signs a real RS256 JWT)."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode("ascii")
    return {
        "type": "service_account",
        "project_id": GPROJECT,
        "client_email": f"deployer@{GPROJECT}.iam.gserviceaccount.com",
        "private_key": pem,
    }


def g404() -> httpx.Response:
    return httpx.Response(404, json={
        "error": {"code": 404, "message": "not found", "status": "NOT_FOUND"},
    })


class FakeGce:
    """Routes MockTransport requests like compute/v1 (and answers the OAuth
    token endpoint + Cloud Resource Manager itself)."""

    def __init__(self, *, exists: bool = False, firewall_conflict: bool = False,
                 statuses: list[tuple[str, str | None]] | None = None,
                 op_polls: int = 2):
        self.exists = exists
        self.firewall_conflict = firewall_conflict
        # Instance GET results front-to-back, holding the last: (status, natIP)
        self.statuses = list(statuses or [
            ("PROVISIONING", None), ("RUNNING", NAT_IP)])
        self.op_polls = op_polls  # operation GETs before status DONE
        self.op_gets = 0
        self.requests: list[tuple[str, str]] = []
        self.insert_body: dict | None = None
        self.firewall_body: dict | None = None

    def _instance(self) -> dict:
        status, ip = self.statuses[0]
        if len(self.statuses) > 1:
            self.statuses.pop(0)
        return {
            "name": GNAME, "status": status,
            "networkInterfaces": [
                {"accessConfigs": [{"natIP": ip}] if ip else []}],
        }

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.host == "oauth2.googleapis.com":
            return httpx.Response(200, json={"access_token": "tok",
                                             "expires_in": 3600})
        if request.url.host == "cloudresourcemanager.googleapis.com":
            return httpx.Response(200, json={"projectId": GPROJECT})
        assert request.url.host == "compute.googleapis.com", request.url.host
        method, path = request.method, request.url.path
        self.requests.append((method, path))
        if method == "GET" and path == f"{INSTANCES_PATH}/{GNAME}":
            if not self.exists:
                return g404()
            return httpx.Response(200, json=self._instance())
        if method == "POST" and path == FIREWALLS_PATH:
            self.firewall_body = json.loads(request.content)
            if self.firewall_conflict:
                return httpx.Response(409, json={"error": {
                    "code": 409, "message": "already exists",
                    "status": "ALREADY_EXISTS"}})
            return httpx.Response(200, json={"name": "op-fw", "status": "DONE"})
        if method == "POST" and path == INSTANCES_PATH:
            self.insert_body = json.loads(request.content)
            self.exists = True
            return httpx.Response(200, json={"name": "op-1", "status": "RUNNING"})
        if method == "GET" and path == OP_PATH:
            self.op_gets += 1
            done = self.op_gets >= self.op_polls
            return httpx.Response(200, json={
                "name": "op-1", "status": "DONE" if done else "RUNNING"})
        if method == "DELETE" and path == f"{INSTANCES_PATH}/{GNAME}":
            if not self.exists:
                return g404()
            self.exists = False
            return httpx.Response(200, json={"name": "op-2", "status": "RUNNING"})
        raise AssertionError(f"unexpected request: {method} {path}")


def gce_target(fake: FakeGce, sa: dict, config: dict | None = None,
               state: dict | None = None) -> GceDbTarget:
    return GceDbTarget(
        creds={"sa": sa},
        config=config if config is not None else dict(BASE_CONFIG),
        state=state or {},
        transport=httpx.MockTransport(fake.handler),
    )


def test_gce_instance_name_sanitizes():
    ctx, _ = make_ctx({})
    assert _instance_name(ctx) == GNAME
    ctx.project_name = "My App!"
    assert _instance_name(ctx) == "homebox-my-app-dev-db"


def test_gce_validate_ok_and_permission_error(sa):
    fake = FakeGce()
    run(gce_target(fake, sa).validate())

    def denied(request: httpx.Request) -> httpx.Response:
        if request.url.host == "oauth2.googleapis.com":
            return httpx.Response(200, json={"access_token": "tok",
                                             "expires_in": 3600})
        return httpx.Response(403, json={"error": {
            "code": 403, "message": "denied", "status": "PERMISSION_DENIED"}})

    t = GceDbTarget(creds={"sa": sa}, config={}, state={},
                    transport=httpx.MockTransport(denied))
    with pytest.raises(TargetError) as exc:
        run(t.validate())
    assert "Compute Admin" in str(exc.value)


def test_gce_fresh_deploy_full_flow(sa):
    fake = FakeGce()
    cfg = dict(BASE_CONFIG)
    ctx, lines = make_ctx(cfg)
    result = run(gce_target(fake, sa, cfg).deploy(ctx))

    fw = fake.firewall_body
    assert fw["name"] == "homebox-db-mesh"
    assert fw["direction"] == "INGRESS"
    assert fw["sourceRanges"] == ["0.0.0.0/0"]
    assert fw["targetTags"] == ["homebox-db"]
    assert fw["allowed"] == [{"IPProtocol": "udp", "ports": ["51820"]}]

    body = fake.insert_body
    assert body["name"] == GNAME
    assert body["machineType"] == f"zones/{ZONE}/machineTypes/e2-small"
    disk = body["disks"][0]
    assert disk["boot"] is True and disk["autoDelete"] is True
    assert disk["initializeParams"]["sourceImage"] == \
        "projects/ubuntu-os-cloud/global/images/family/ubuntu-2404-lts-amd64"
    assert body["networkInterfaces"][0]["accessConfigs"][0]["type"] == \
        "ONE_TO_ONE_NAT"
    assert body["tags"] == {"items": ["homebox-db"]}
    items = {i["key"]: i["value"] for i in body["metadata"]["items"]}
    assert items["user-data"] == render_cloud_init(spec_from_config(cfg))
    assert "Address = 10.77.240.0/16" in items["user-data"]

    # The zone operation was polled to DONE, then the instance to RUNNING.
    assert fake.op_gets == 2
    assert result.endpoint == NAT_IP
    assert result.cname_target is None
    assert result.proxied is False
    assert result.state["instance_name"] == GNAME
    assert result.state["zone"] == ZONE
    assert result.state["project"] == GPROJECT
    assert result.state["public_ip"] == NAT_IP
    assert result.state["mesh"] == {
        "ordinal": ORDINAL, "ip": "10.77.240.0", "wg_pubkey": "VMPUBKEY=",
        "endpoint": f"{NAT_IP}:51820",
    }
    assert result.state["db"] == {"port": 5432, "node_name": "n61440"}
    assert any("creating GCE instance" in ln for ln in lines)


def test_gce_open_pg_public_adds_5432_to_firewall(sa):
    fake = FakeGce()
    cfg = dict(BASE_CONFIG, open_pg_public=True)
    ctx, _ = make_ctx(cfg)
    run(gce_target(fake, sa, cfg).deploy(ctx))
    assert fake.firewall_body["allowed"] == [
        {"IPProtocol": "udp", "ports": ["51820"]},
        {"IPProtocol": "tcp", "ports": ["5432"]},
    ]


def test_gce_firewall_conflict_tolerated(sa):
    fake = FakeGce(firewall_conflict=True)
    ctx, _ = make_ctx(dict(BASE_CONFIG))
    result = run(gce_target(fake, sa).deploy(ctx))
    assert fake.insert_body is not None  # instance still created
    assert result.endpoint == NAT_IP


def test_gce_idempotent_second_deploy(sa):
    fake = FakeGce(exists=True, statuses=[("RUNNING", NAT_IP)])
    ctx, _ = make_ctx(dict(BASE_CONFIG))
    result = run(gce_target(fake, sa).deploy(ctx))
    posts = [(m, p) for m, p in fake.requests if m == "POST"]
    assert posts == []  # no firewall insert, no instance insert
    assert result.endpoint == NAT_IP
    assert result.state["instance_name"] == GNAME
    assert result.state["mesh"]["endpoint"] == f"{NAT_IP}:51820"


def test_gce_destroy_and_404_ok(sa):
    fake = FakeGce(exists=True)
    run(gce_target(fake, sa).destroy(
        {"instance_name": GNAME, "zone": ZONE}))
    assert ("DELETE", f"{INSTANCES_PATH}/{GNAME}") in fake.requests

    gone = FakeGce(exists=False)
    run(gce_target(gone, sa).destroy(
        {"instance_name": GNAME, "zone": ZONE}))  # 404 → no raise

    untouched = FakeGce()
    run(gce_target(untouched, sa).destroy({}))
    run(gce_target(untouched, sa).destroy(None))
    assert untouched.requests == []


def test_gce_probe_paths(sa):
    up = FakeGce(exists=True, statuses=[("RUNNING", NAT_IP)])
    assert run(gce_target(up, sa).probe(
        {"instance_name": GNAME, "zone": ZONE})) is True

    provisioning = FakeGce(exists=True, statuses=[("PROVISIONING", None)])
    assert run(gce_target(provisioning, sa).probe(
        {"instance_name": GNAME, "zone": ZONE})) is False

    missing = FakeGce(exists=False)
    assert run(gce_target(missing, sa).probe(
        {"instance_name": GNAME, "zone": ZONE})) is False
    assert run(gce_target(missing, sa).probe({})) is False
