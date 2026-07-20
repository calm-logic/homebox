"""Route tests for the AWS / GCP cloud-account integrations: connect (validate
via STS / Cloud Resource Manager, upsert an encrypted Integration row) and the
disconnect guard that blocks removal while ServiceTargets still deploy through
the account.

Runs on in-memory sqlite (aiosqlite) against a minimal FastAPI app that mounts
only the integrations router — the cloud validation calls are monkeypatched on
AwsClient / GcpClient, so no network and no credentials.
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from app.auth import require_session_api  # noqa: E402
from app.crypto import decrypt  # noqa: E402
from app.db import Base, get_session  # noqa: E402
from app.models import Integration, Project, Service, ServiceTarget  # noqa: E402
from app.routes import integrations as integrations_routes  # noqa: E402
from app.targets.awslib import AwsClient, AwsError  # noqa: E402
from app.targets.gcplib import GcpClient, GcpError  # noqa: E402

_ENGINES: list = []


def run(coro):
    async def main():
        try:
            return await coro
        finally:
            while _ENGINES:
                await _ENGINES.pop().dispose()
    return asyncio.run(main())


async def make_app():
    """Fresh sqlite DB + FastAPI app mounting just the integrations router,
    with auth and DB dependencies overridden. Returns (client, sessionmaker);
    the client drives routes, the sessionmaker inspects rows directly."""
    engine = create_async_engine("sqlite+aiosqlite://")
    _ENGINES.append(engine)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)

    app = FastAPI()
    app.include_router(integrations_routes.router)

    async def override_session():
        async with maker() as s:
            yield s

    app.dependency_overrides[get_session] = override_session
    app.dependency_overrides[require_session_api] = lambda: "tester"

    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )
    return client, maker


# ── fixtures: fake cloud validation ───────────────────────────────────────────

AWS_IDENTITY = {
    "account": "123456789012",
    "arn": "arn:aws:iam::123456789012:user/homebox",
    "user_id": "AIDAEXAMPLE",
}

SA = {
    "type": "service_account",
    "project_id": "my-proj",
    "client_email": "deploy@my-proj.iam.gserviceaccount.com",
    "private_key": "-----BEGIN PRIVATE KEY-----\nfake\n-----END PRIVATE KEY-----\n",
}


@pytest.fixture
def aws_ok(monkeypatch):
    async def fake_sts(self):
        return dict(AWS_IDENTITY)
    monkeypatch.setattr(AwsClient, "sts_get_caller_identity", fake_sts)


@pytest.fixture
def aws_bad(monkeypatch):
    async def fake_sts(self):
        raise AwsError(403, "InvalidClientTokenId",
                       "The security token included in the request is invalid.")
    monkeypatch.setattr(AwsClient, "sts_get_caller_identity", fake_sts)


@pytest.fixture
def gcp_ok(monkeypatch):
    async def fake_get_project(self):
        return {"projectId": self.project_id, "projectNumber": "987654321098",
                "lifecycleState": "ACTIVE"}
    monkeypatch.setattr(GcpClient, "get_project", fake_get_project)


@pytest.fixture
def gcp_bad(monkeypatch):
    async def fake_get_project(self):
        raise GcpError(403, "Permission denied on project (PERMISSION_DENIED)",
                       "PERMISSION_DENIED")
    monkeypatch.setattr(GcpClient, "get_project", fake_get_project)


async def connect_aws(client, **overrides):
    body = {"access_key_id": "AKIAEXAMPLE", "secret_access_key": "sekret",
            "region": "eu-west-1", **overrides}
    return await client.post("/api/integrations/aws/connect", json=body)


async def connect_gcp(client, sa=SA, region="europe-west4"):
    return await client.post("/api/integrations/gcp/connect", json={
        "service_account_json": json.dumps(sa), "region": region,
    })


# ── AWS connect ───────────────────────────────────────────────────────────────

def test_aws_connect_creates_encrypted_row(aws_ok):
    async def body():
        client, maker = await make_app()
        r = await connect_aws(client)
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["provider"] == "aws"
        assert data["account_login"] == "123456789012"
        assert data["name"] == "AWS 123456789012"
        assert data["status"] == "connected"
        assert data["source"] == "keys"

        async with maker() as s:
            rows = (await s.execute(select(Integration))).scalars().all()
            assert len(rows) == 1
            row = rows[0]
            assert row.secret_encrypted != "AKIAEXAMPLE:sekret"  # encrypted at rest
            assert decrypt(row.secret_encrypted) == "AKIAEXAMPLE:sekret"
            assert row.account_id == "123456789012"
            assert row.config == {"region": "eu-west-1", "arn": AWS_IDENTITY["arn"]}
    run(body())


def test_aws_region_defaults(aws_ok):
    async def body():
        client, maker = await make_app()
        r = await connect_aws(client, region="  ")
        assert r.status_code == 200
        async with maker() as s:
            row = (await s.execute(select(Integration))).scalar_one()
            assert row.config["region"] == "us-east-1"
    run(body())


def test_aws_empty_fields_rejected(aws_ok):
    async def body():
        client, _ = await make_app()
        r = await connect_aws(client, secret_access_key="   ")
        assert r.status_code == 400
    run(body())


def test_aws_bad_creds_400_no_row(aws_bad):
    async def body():
        client, maker = await make_app()
        r = await connect_aws(client)
        assert r.status_code == 400
        assert "security token" in r.json()["detail"]
        async with maker() as s:
            assert (await s.execute(select(Integration))).scalars().all() == []
    run(body())


def test_aws_reconnect_upserts(aws_ok):
    async def body():
        client, maker = await make_app()
        assert (await connect_aws(client)).status_code == 200
        r = await connect_aws(client, secret_access_key="rotated", region="us-west-2")
        assert r.status_code == 200
        async with maker() as s:
            rows = (await s.execute(select(Integration))).scalars().all()
            assert len(rows) == 1  # same account → updated, not duplicated
            assert decrypt(rows[0].secret_encrypted) == "AKIAEXAMPLE:rotated"
            assert rows[0].config["region"] == "us-west-2"
            assert rows[0].status == "connected"
    run(body())


# ── GCP connect ───────────────────────────────────────────────────────────────

def test_gcp_connect_stores_project(gcp_ok):
    async def body():
        client, maker = await make_app()
        r = await connect_gcp(client)
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["provider"] == "gcp"
        assert data["account_login"] == "my-proj"
        assert data["account_id"] == "987654321098"  # project number
        assert data["name"] == "GCP my-proj"
        assert data["source"] == "service-account"

        async with maker() as s:
            row = (await s.execute(select(Integration))).scalar_one()
            assert json.loads(decrypt(row.secret_encrypted)) == SA  # raw key stored
            assert row.config == {"region": "europe-west4",
                                  "client_email": SA["client_email"]}
    run(body())


def test_gcp_invalid_json_400(gcp_ok):
    async def body():
        client, maker = await make_app()
        r = await client.post("/api/integrations/gcp/connect",
                              json={"service_account_json": "not json {"})
        assert r.status_code == 400
        assert "JSON" in r.json()["detail"]
        async with maker() as s:
            assert (await s.execute(select(Integration))).scalars().all() == []
    run(body())


def test_gcp_missing_fields_400(gcp_ok):
    async def body():
        client, _ = await make_app()
        sa = {k: v for k, v in SA.items() if k != "private_key"}
        r = await connect_gcp(client, sa=sa)
        assert r.status_code == 400
        assert "private_key" in r.json()["detail"]
    run(body())


def test_gcp_bad_creds_400(gcp_bad):
    async def body():
        client, _ = await make_app()
        r = await connect_gcp(client)
        assert r.status_code == 400
        assert "PERMISSION_DENIED" in r.json()["detail"]
    run(body())


def test_gcp_reconnect_upserts(gcp_ok):
    async def body():
        client, maker = await make_app()
        assert (await connect_gcp(client)).status_code == 200
        assert (await connect_gcp(client, region="us-central1")).status_code == 200
        async with maker() as s:
            rows = (await s.execute(select(Integration))).scalars().all()
            assert len(rows) == 1
            assert rows[0].config["region"] == "us-central1"
    run(body())


# ── disconnect guard ──────────────────────────────────────────────────────────

def test_disconnect_blocked_while_targets_reference_account(aws_ok):
    async def body():
        client, maker = await make_app()
        integ_id = (await connect_aws(client)).json()["id"]

        async with maker() as s:
            p = Project(repo_full_name="al/app", name="app", managed=False)
            s.add(p)
            await s.flush()
            svc = Service(project_id=p.id, name="web", kind="web")
            s.add(svc)
            await s.flush()
            st = ServiceTarget(service_id=svc.id, target="aws",
                               integration_id=integ_id, config={})
            s.add(st)
            await s.commit()
            st_id = st.id

        r = await client.delete(f"/api/integrations/{integ_id}")
        assert r.status_code == 409
        assert "retarget" in r.json()["detail"]
        async with maker() as s:
            assert await s.get(Integration, integ_id) is not None  # row survived

        # Retarget (drop the reference) → disconnect proceeds.
        async with maker() as s:
            await s.delete(await s.get(ServiceTarget, st_id))
            await s.commit()

        r = await client.delete(f"/api/integrations/{integ_id}")
        assert r.status_code == 200, r.text
        async with maker() as s:
            assert await s.get(Integration, integ_id) is None
    run(body())
