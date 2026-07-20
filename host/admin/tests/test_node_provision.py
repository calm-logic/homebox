"""Tests for the cloud node provisioner (app/nodeprovision.py + the
/api/cluster/account/nodes/provision routes).

Runs on in-memory sqlite (aiosqlite) against a minimal FastAPI app that
mounts only the provision router. The cloud APIs are httpx.MockTransport
fakes (EC2 Query protocol XML / compute-v1 JSON, same style as
test_db_vm_targets.py) injected via nodeprovision._TRANSPORT; the
control-plane join-token mint is a monkeypatched clusterlib._cp. No network,
no credentials.
"""
from __future__ import annotations

import asyncio
import base64
import sys
import urllib.parse
from datetime import datetime, timedelta
from pathlib import Path

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from app import clusterlib, crypto, nodeprovision  # noqa: E402
from app.auth import require_session_api  # noqa: E402
from app.db import Base, get_session  # noqa: E402
from app.models import Integration  # noqa: E402
from app.routes import provision as provision_routes  # noqa: E402

JOIN_TOKEN = "hbj.c1.sekrit"
CP_URL = "http://cp.test"
IID = "i-0node1234"
SG_ID = "sg-0node"
GPROJECT = "proj-1"
ZONE = "us-central1-a"

_ENGINES: list = []


def run(coro):
    async def main():
        try:
            return await coro
        finally:
            while _ENGINES:
                await _ENGINES.pop().dispose()
    return asyncio.run(main())


@pytest.fixture(scope="module")
def sa_pem() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode("ascii")


# ───── harness ────────────────────────────────────────────────────────────────


async def make_app():
    engine = create_async_engine("sqlite+aiosqlite://")
    _ENGINES.append(engine)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)

    app = FastAPI()
    app.include_router(provision_routes.router)

    async def override_session():
        async with maker() as s:
            yield s

    app.dependency_overrides[get_session] = override_session
    app.dependency_overrides[require_session_api] = lambda: "tester"

    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )
    return client, maker


async def seed(
    maker, *, account: bool = True, cluster: bool = True,
    features: list[str] | None = None, sa_pem: str | None = None,
    roster: list[dict] | None = None,
) -> dict[str, int]:
    """Integrations + account/cluster settings. Returns integration ids."""
    async with maker() as s:
        aws = Integration(provider="aws", account_id="123456789012",
                          secret_encrypted=crypto.encrypt("AKID:sk"))
        s.add(aws)
        ids = {}
        if sa_pem:
            import json as _j
            gcp = Integration(provider="gcp", account_id=GPROJECT,
                              secret_encrypted=crypto.encrypt(_j.dumps({
                                  "type": "service_account",
                                  "project_id": GPROJECT,
                                  "client_email": f"d@{GPROJECT}.iam.gserviceaccount.com",
                                  "private_key": sa_pem,
                              })))
            s.add(gcp)
        await s.flush()
        ids["aws"] = aws.id
        if sa_pem:
            ids["gcp"] = gcp.id
        if account:
            await clusterlib._set_setting(s, clusterlib.ACCOUNT_KEY, {
                "control_plane_url": CP_URL,
                "token_encrypted": crypto.encrypt("hba.account"),
                "node_name": "founder", "peer_url": "http://10.0.0.1",
            })
        if cluster:
            await clusterlib._set_setting(s, clusterlib.CLUSTER_KEY, {
                "cluster_id": "c1",
                "control_plane_url": CP_URL,
                "account_token_encrypted": crypto.encrypt("hba.account"),
                "node_token_encrypted": crypto.encrypt("hbn.node"),
                "roster": roster if roster is not None else [
                    {"node_id": "n0", "name": "founder"}],
                "license": {
                    "plan": "pro",
                    "features": features if features is not None else ["cluster"],
                },
                "license_verified": True,
            })
        await s.commit()
        return ids


@pytest.fixture
def cp_calls(monkeypatch):
    """Fake control plane: records join-token mints, returns JOIN_TOKEN."""
    calls: list[tuple[str, str, str, str]] = []

    async def fake_cp(method, base, path, *, token=None, body=None):
        calls.append((method, base, path, token or ""))
        assert path.endswith("/join-tokens")
        return {"join_token": JOIN_TOKEN}

    monkeypatch.setattr(clusterlib, "_cp", fake_cp)
    return calls


# ───── cloud fakes ────────────────────────────────────────────────────────────

_AMIS_XML = (
    '<DescribeImagesResponse xmlns="http://ec2.amazonaws.com/doc/2016-11-15/">'
    "<imagesSet>"
    "<item><imageId>ami-old</imageId>"
    "<creationDate>2024-01-01T00:00:00.000Z</creationDate></item>"
    "<item><imageId>ami-new</imageId>"
    "<creationDate>2025-06-01T00:00:00.000Z</creationDate></item>"
    "</imagesSet></DescribeImagesResponse>"
).encode()


def _instances_xml(instances: list[dict]) -> bytes:
    items = "".join(
        "<item>"
        f"<instanceId>{i['id']}</instanceId>"
        f"<instanceState><code>0</code><name>{i['state']}</name></instanceState>"
        "</item>"
        for i in instances
    )
    return (
        '<DescribeInstancesResponse xmlns="http://ec2.amazonaws.com/doc/2016-11-15/">'
        f"<reservationSet><item><instancesSet>{items}</instancesSet></item>"
        "</reservationSet></DescribeInstancesResponse>"
    ).encode() if instances else (
        b"<DescribeInstancesResponse><reservationSet/></DescribeInstancesResponse>"
    )


class FakeEc2:
    """Routes MockTransport requests like the EC2 Query API."""

    def __init__(self, *, existing: dict | None = None,
                 by_id_state: str = "running"):
        self.existing = existing   # tag-filter DescribeInstances result
        self.by_id_state = by_id_state
        self.calls: list[tuple[str, dict]] = []

    def actions(self) -> list[str]:
        return [a for a, _ in self.calls]

    def payload(self, action: str) -> dict:
        return next(f for a, f in self.calls if a == action)

    def payloads(self, action: str) -> list[dict]:
        return [f for a, f in self.calls if a == action]

    def handler(self, request: httpx.Request) -> httpx.Response:
        assert request.url.host.startswith("ec2."), request.url.host
        form = dict(urllib.parse.parse_qsl(request.content.decode()))
        action = form["Action"]
        self.calls.append((action, form))
        if action == "DescribeImages":
            return httpx.Response(200, content=_AMIS_XML)
        if action == "DescribeInstances":
            if "InstanceId.1" in form:
                return httpx.Response(200, content=_instances_xml(
                    [{"id": form["InstanceId.1"], "state": self.by_id_state}]))
            return httpx.Response(200, content=_instances_xml(
                [self.existing] if self.existing else []))
        if action == "CreateSecurityGroup":
            return httpx.Response(200, content=(
                "<CreateSecurityGroupResponse><return>true</return>"
                f"<groupId>{SG_ID}</groupId></CreateSecurityGroupResponse>"
            ).encode())
        if action == "AuthorizeSecurityGroupIngress":
            return httpx.Response(200, content=(
                b"<AuthorizeSecurityGroupIngressResponse><return>true</return>"
                b"</AuthorizeSecurityGroupIngressResponse>"))
        if action == "RunInstances":
            return httpx.Response(200, content=(
                "<RunInstancesResponse><instancesSet><item>"
                f"<instanceId>{IID}</instanceId>"
                "<instanceState><code>0</code><name>pending</name></instanceState>"
                "</item></instancesSet></RunInstancesResponse>"
            ).encode())
        if action == "TerminateInstances":
            return httpx.Response(200, content=(
                b"<TerminateInstancesResponse><instancesSet/>"
                b"</TerminateInstancesResponse>"))
        if action == "DeleteSecurityGroup":
            return httpx.Response(200, content=(
                b"<DeleteSecurityGroupResponse><return>true</return>"
                b"</DeleteSecurityGroupResponse>"))
        raise AssertionError(f"unexpected EC2 action: {action}")


class FakeGce:
    """Routes MockTransport requests like compute/v1 + the OAuth endpoint."""

    def __init__(self, *, exists: bool = False, status: str = "RUNNING",
                 deleted: bool = False):
        self.exists = exists      # instance GET hits before any insert
        self.status = status
        self.deleted = deleted    # GET/DELETE return 404
        self.requests: list[tuple[str, str]] = []
        self.insert_body: dict | None = None
        self.firewall_body: dict | None = None
        self.inserted = False

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.host == "oauth2.googleapis.com":
            return httpx.Response(200, json={"access_token": "tok",
                                             "expires_in": 3600})
        path = request.url.path
        self.requests.append((request.method, path))
        base = f"/compute/v1/projects/{GPROJECT}/zones/{ZONE}/instances"
        if path == f"{base}" and request.method == "POST":
            import json as _j
            self.insert_body = _j.loads(request.content)
            self.inserted = True
            return httpx.Response(200, json={"name": "op-1", "status": "RUNNING"})
        if path.startswith(f"{base}/"):
            if request.method == "DELETE":
                if self.deleted:
                    return httpx.Response(404, json={"error": {
                        "code": 404, "message": "gone", "status": "NOT_FOUND"}})
                return httpx.Response(200, json={"name": "op-del"})
            if self.deleted or not (self.exists or self.inserted):
                return httpx.Response(404, json={"error": {
                    "code": 404, "message": "not found", "status": "NOT_FOUND"}})
            return httpx.Response(200, json={"name": path.rsplit("/", 1)[-1],
                                             "status": self.status})
        if path == f"/compute/v1/projects/{GPROJECT}/global/firewalls":
            import json as _j
            self.firewall_body = _j.loads(request.content)
            return httpx.Response(200, json={"name": "op-fw"})
        raise AssertionError(f"unexpected GCE request: {request.method} {path}")


@pytest.fixture
def ec2(monkeypatch):
    fake = FakeEc2()
    monkeypatch.setattr(nodeprovision, "_TRANSPORT",
                        httpx.MockTransport(fake.handler))
    return fake


@pytest.fixture
def gce(monkeypatch):
    fake = FakeGce()
    monkeypatch.setattr(nodeprovision, "_TRANSPORT",
                        httpx.MockTransport(fake.handler))
    return fake


AWS_BODY = {"name": "edge", "provider": "aws",
            "region": "us-east-1"}  # + integration_id per test


# ───── happy paths ────────────────────────────────────────────────────────────


def test_provision_aws_happy_path(cp_calls, ec2):
    async def t():
        client, maker = await make_app()
        ids = await seed(maker)
        r = await client.post("/api/cluster/account/nodes/provision",
                              json={**AWS_BODY, "integration_id": ids["aws"]})
        assert r.status_code == 200, r.text
        entry = r.json()
        assert entry["status"] == "booting"
        assert entry["provider"] == "aws"
        assert entry["name"] == "edge"
        assert entry["machine"] == "t3.small"
        assert entry["resource"]["instance_id"] == IID
        assert entry["resource"]["sg_id"] == SG_ID
        assert entry["roster_before"] == ["n0"]

        # Join token minted with the account credential against our cluster.
        assert cp_calls == [
            ("POST", CP_URL, "/v1/clusters/c1/join-tokens", "hba.account")]

        # Newest Ubuntu AMI resolved per region; deterministic name + tag.
        runp = ec2.payload("RunInstances")
        assert runp["ImageId"] == "ami-new"
        assert runp["InstanceType"] == "t3.small"
        assert runp["TagSpecification.1.Tag.1.Key"] == "homebox-node"
        assert runp["TagSpecification.1.Tag.1.Value"] == "homebox-node-edge"
        assert runp["TagSpecification.1.Tag.2.Value"] == "homebox-node-edge"

        # Peer ports open: 80/443/tcp + 51820/udp.
        rules = {(f["IpPermissions.1.IpProtocol"], f["IpPermissions.1.FromPort"])
                 for f in ec2.payloads("AuthorizeSecurityGroupIngress")}
        assert rules == {("tcp", "80"), ("tcp", "443"), ("udp", "51820")}

        # The startup script joins with the token as a FULL peer (no mirror
        # role) and derives peer_url from the instance's public IP.
        script = base64.b64decode(runp["UserData"]).decode()
        assert JOIN_TOKEN in script
        assert "NODE_NAME=edge" in script
        assert f"CONTROL_PLANE_URL={CP_URL}" in script
        assert "/api/cluster/join" in script
        assert "HOMEBOX_NODE_ROLE" not in script
        assert "mirror" not in script.lower()
        assert "169.254.169.254" in script  # AWS IMDS public-IP probe
        assert "secrets.json" in script     # pre-seeded admin credentials

        # GET surfaces it (still booting — roster unchanged).
        r = await client.get("/api/cluster/account/nodes/provision")
        assert r.status_code == 200
        provisions = r.json()["provisions"]
        assert len(provisions) == 1
        assert provisions[0]["id"] == entry["id"]
        assert provisions[0]["status"] == "booting"
        await client.aclose()
    run(t())


def test_provision_gcp_happy_path(cp_calls, gce, sa_pem):
    async def t():
        client, maker = await make_app()
        ids = await seed(maker, sa_pem=sa_pem)
        r = await client.post("/api/cluster/account/nodes/provision", json={
            "name": "edge", "provider": "gcp",
            "integration_id": ids["gcp"], "region": "us-central1",
        })
        assert r.status_code == 200, r.text
        entry = r.json()
        assert entry["status"] == "booting"
        assert entry["machine"] == "e2-small"
        assert entry["resource"] == {
            "instance_name": "homebox-node-edge", "zone": ZONE,
            "project": GPROJECT,
        }

        body = gce.insert_body
        assert body["name"] == "homebox-node-edge"
        assert body["machineType"].endswith("/machineTypes/e2-small")
        assert "ubuntu-2404-lts" in body["disks"][0]["initializeParams"]["sourceImage"]
        assert body["tags"] == {"items": ["homebox-node"]}

        # Firewall for the peer ports, tolerating first-writer-wins 409s.
        fw = gce.firewall_body
        assert fw["name"] == "homebox-node-mesh"
        assert {"IPProtocol": "udp", "ports": ["51820"]} in fw["allowed"]
        assert {"IPProtocol": "tcp", "ports": ["80", "443"]} in fw["allowed"]

        # Startup script delivered via the GCE startup-script metadata key.
        meta = {i["key"]: i["value"] for i in body["metadata"]["items"]}
        script = meta["startup-script"]
        assert JOIN_TOKEN in script
        assert "HOMEBOX_NODE_ROLE" not in script
        assert "mirror" not in script.lower()
        assert "metadata.google.internal" in script  # GCE public-IP probe
        await client.aclose()
    run(t())


# ───── join detection / error paths ───────────────────────────────────────────


async def _set_cluster_roster(maker, roster: list[dict]) -> None:
    async with maker() as s:
        state = await clusterlib._get_setting(s, clusterlib.CLUSTER_KEY)
        state = dict(state or {})
        state["roster"] = roster
        await clusterlib._set_setting(s, clusterlib.CLUSTER_KEY, state)
        await s.commit()


def test_join_detected_via_roster(cp_calls, ec2):
    async def t():
        client, maker = await make_app()
        ids = await seed(maker)
        r = await client.post("/api/cluster/account/nodes/provision",
                              json={**AWS_BODY, "integration_id": ids["aws"]})
        assert r.status_code == 200

        # A NEW roster node named like the provision → joined.
        await _set_cluster_roster(maker, [
            {"node_id": "n0", "name": "founder"},
            {"node_id": "n-new", "name": "edge"},
        ])
        r = await client.get("/api/cluster/account/nodes/provision")
        entry = r.json()["provisions"][0]
        assert entry["status"] == "joined"
        assert entry["node_id"] == "n-new"

        # Terminal — stays joined on subsequent refreshes.
        r = await client.get("/api/cluster/account/nodes/provision")
        assert r.json()["provisions"][0]["status"] == "joined"
        await client.aclose()
    run(t())


def test_join_requires_new_node_id(cp_calls, ec2):
    """A pre-existing node that happens to share the name must not count."""
    async def t():
        client, maker = await make_app()
        ids = await seed(maker, roster=[{"node_id": "n0", "name": "edge"}])
        r = await client.post("/api/cluster/account/nodes/provision",
                              json={**AWS_BODY, "integration_id": ids["aws"]})
        assert r.status_code == 200
        r = await client.get("/api/cluster/account/nodes/provision")
        assert r.json()["provisions"][0]["status"] == "booting"
        await client.aclose()
    run(t())


async def _age_entry(maker, minutes: int) -> None:
    async with maker() as s:
        entries = await clusterlib._get_setting(s, nodeprovision.PROVISIONS_KEY)
        entries = [dict(e) for e in entries]
        for e in entries:
            e["created_at"] = (
                datetime.utcnow() - timedelta(minutes=minutes)).isoformat()
        await clusterlib._set_setting(s, nodeprovision.PROVISIONS_KEY, entries)
        await s.commit()


def test_timeout_marks_error(cp_calls, ec2):
    async def t():
        client, maker = await make_app()
        ids = await seed(maker)
        r = await client.post("/api/cluster/account/nodes/provision",
                              json={**AWS_BODY, "integration_id": ids["aws"]})
        assert r.status_code == 200
        await _age_entry(maker, 31)
        r = await client.get("/api/cluster/account/nodes/provision")
        entry = r.json()["provisions"][0]
        assert entry["status"] == "error"
        assert "did not join" in entry["error"]
        await client.aclose()
    run(t())


def test_dead_instance_marks_error(cp_calls, ec2):
    async def t():
        client, maker = await make_app()
        ids = await seed(maker)
        r = await client.post("/api/cluster/account/nodes/provision",
                              json={**AWS_BODY, "integration_id": ids["aws"]})
        assert r.status_code == 200
        ec2.by_id_state = "terminated"  # VM died before joining
        r = await client.get("/api/cluster/account/nodes/provision")
        entry = r.json()["provisions"][0]
        assert entry["status"] == "error"
        assert "terminated" in entry["error"]
        await client.aclose()
    run(t())


# ───── teardown ───────────────────────────────────────────────────────────────


def test_teardown_aws_deletes_instance(cp_calls, ec2):
    async def t():
        client, maker = await make_app()
        ids = await seed(maker)
        r = await client.post("/api/cluster/account/nodes/provision",
                              json={**AWS_BODY, "integration_id": ids["aws"]})
        pid = r.json()["id"]
        r = await client.delete(f"/api/cluster/account/nodes/provision/{pid}")
        assert r.status_code == 200
        assert r.json()["ok"] is True
        term = ec2.payload("TerminateInstances")
        assert term["InstanceId.1"] == IID
        assert "DeleteSecurityGroup" in ec2.actions()

        r = await client.get("/api/cluster/account/nodes/provision")
        assert r.json()["provisions"] == []

        # Unknown id → 404.
        r = await client.delete(f"/api/cluster/account/nodes/provision/{pid}")
        assert r.status_code == 404
        await client.aclose()
    run(t())


def test_teardown_gcp_tolerates_missing_instance(cp_calls, gce, sa_pem):
    async def t():
        client, maker = await make_app()
        ids = await seed(maker, sa_pem=sa_pem)
        r = await client.post("/api/cluster/account/nodes/provision", json={
            "name": "edge", "provider": "gcp",
            "integration_id": ids["gcp"], "region": "us-central1",
        })
        pid = r.json()["id"]
        gce.deleted = True  # instance already gone → DELETE 404, still ok
        r = await client.delete(f"/api/cluster/account/nodes/provision/{pid}")
        assert r.status_code == 200
        assert ("DELETE",
                f"/compute/v1/projects/{GPROJECT}/zones/{ZONE}/instances/"
                "homebox-node-edge") in gce.requests

        r = await client.get("/api/cluster/account/nodes/provision")
        assert r.json()["provisions"] == []
        await client.aclose()
    run(t())


# ───── gating ─────────────────────────────────────────────────────────────────


def test_unlinked_account_412(cp_calls, ec2):
    async def t():
        client, maker = await make_app()
        ids = await seed(maker, account=False)
        r = await client.post("/api/cluster/account/nodes/provision",
                              json={**AWS_BODY, "integration_id": ids["aws"]})
        assert r.status_code == 412
        assert "account" in r.json()["detail"].lower()
        assert cp_calls == []
        assert ec2.calls == []
        await client.aclose()
    run(t())


def test_no_cluster_412(cp_calls, ec2):
    async def t():
        client, maker = await make_app()
        ids = await seed(maker, cluster=False)
        r = await client.post("/api/cluster/account/nodes/provision",
                              json={**AWS_BODY, "integration_id": ids["aws"]})
        assert r.status_code == 412
        assert "cluster" in r.json()["detail"].lower()
        assert cp_calls == []
        assert ec2.calls == []
        await client.aclose()
    run(t())


def test_free_plan_402(cp_calls, ec2):
    async def t():
        client, maker = await make_app()
        ids = await seed(maker, features=[])
        r = await client.post("/api/cluster/account/nodes/provision",
                              json={**AWS_BODY, "integration_id": ids["aws"]})
        assert r.status_code == 402
        assert cp_calls == []
        assert ec2.calls == []
        await client.aclose()
    run(t())


def test_wrong_provider_integration_400(cp_calls, ec2):
    async def t():
        client, maker = await make_app()
        ids = await seed(maker)
        r = await client.post("/api/cluster/account/nodes/provision", json={
            "name": "edge", "provider": "gcp",
            "integration_id": ids["aws"], "region": "us-central1",
        })
        assert r.status_code == 400
        assert "aws" in r.json()["detail"]
        await client.aclose()
    run(t())


def test_unknown_integration_404(cp_calls, ec2):
    async def t():
        client, maker = await make_app()
        await seed(maker)
        r = await client.post("/api/cluster/account/nodes/provision",
                              json={**AWS_BODY, "integration_id": 999})
        assert r.status_code == 404
        await client.aclose()
    run(t())


# ───── idempotency ────────────────────────────────────────────────────────────


def test_reprovision_same_name_does_not_double_create(cp_calls, ec2):
    async def t():
        client, maker = await make_app()
        ids = await seed(maker)
        body = {**AWS_BODY, "integration_id": ids["aws"]}
        r1 = await client.post("/api/cluster/account/nodes/provision", json=body)
        r2 = await client.post("/api/cluster/account/nodes/provision", json=body)
        assert r1.status_code == r2.status_code == 200
        assert r1.json()["id"] == r2.json()["id"]
        # One VM, one token mint, one list entry.
        assert ec2.actions().count("RunInstances") == 1
        assert len(cp_calls) == 1
        r = await client.get("/api/cluster/account/nodes/provision")
        assert len(r.json()["provisions"]) == 1
        await client.aclose()
    run(t())


def test_errored_entry_can_be_reprovisioned(cp_calls, ec2):
    """After an error the same name provisions again — and even then the
    cloud side adopts the still-tagged instance rather than duplicating."""
    async def t():
        client, maker = await make_app()
        ids = await seed(maker)
        body = {**AWS_BODY, "integration_id": ids["aws"]}
        r = await client.post("/api/cluster/account/nodes/provision", json=body)
        assert r.status_code == 200
        await _age_entry(maker, 31)
        await client.get("/api/cluster/account/nodes/provision")  # → error

        ec2.existing = {"id": IID, "state": "running"}  # tag lookup now hits
        r = await client.post("/api/cluster/account/nodes/provision", json=body)
        assert r.status_code == 200
        assert r.json()["status"] == "booting"
        # Second provision adopted the tagged instance — still one RunInstances.
        assert ec2.actions().count("RunInstances") == 1
        r = await client.get("/api/cluster/account/nodes/provision")
        provisions = r.json()["provisions"]
        assert {p["status"] for p in provisions} == {"error", "booting"}
        await client.aclose()
    run(t())


# ───── unit: startup script rendering ─────────────────────────────────────────


def test_render_startup_script_quotes_and_provider_probe():
    aws = nodeprovision.render_startup_script(
        provider="aws", join_token="hbj.c.$(evil)", control_plane_url=CP_URL,
        node_name="edge", install_url="https://homebox.sh/install.sh")
    assert "JOIN_TOKEN='hbj.c.$(evil)'" in aws  # shell-quoted, not expanded
    assert "169.254.169.254" in aws
    assert "metadata.google.internal" not in aws

    gcp = nodeprovision.render_startup_script(
        provider="gcp", join_token="t", control_plane_url=CP_URL,
        node_name="edge")
    assert "metadata.google.internal" in gcp
    assert "169.254.169.254" not in gcp
    assert "install.sh" in gcp  # default install URL from settings

    with pytest.raises(nodeprovision.NodeProvisionError):
        nodeprovision.render_startup_script(
            provider="azure", join_token="t", control_plane_url=CP_URL,
            node_name="x")
