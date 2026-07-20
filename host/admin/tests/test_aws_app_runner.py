"""Tests for the AWS App Runner target (app/targets/aws_app_runner.py).

All AWS behavior — IAM role bootstrap (Query protocol, global endpoint),
create vs update vs redeploy, RUNNING polling, custom domains with ACM
validation records, destroy, probe — runs against httpx.MockTransport with a
routed fake. registry.ecr_push and the docker helpers are monkeypatched; the
poll interval is zeroed. No network, no docker, no real credentials.
"""
from __future__ import annotations

import asyncio
import json
import sys
import urllib.parse
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app.targets.aws_app_runner as apprunner_mod  # noqa: E402
import app.targets.registry as registry  # noqa: E402
from app.targets.aws_app_runner import AppRunnerTarget, _service_name  # noqa: E402
from app.targets.base import TargetDeployCtx, TargetError  # noqa: E402

ACCOUNT = "123456789012"
NAME = "homebox-listless-dev-web"
ARN = f"arn:aws:apprunner:us-east-2:{ACCOUNT}:service/{NAME}/svc-1"
ROLE_ARN = f"arn:aws:iam::{ACCOUNT}:role/homebox-apprunner-ecr-access"
POLICY_ARN = (
    "arn:aws:iam::aws:policy/service-role/AWSAppRunnerServicePolicyForECRAccess"
)
SERVICE_URL = "abc123.us-east-2.awsapprunner.com"
DNS_TARGET = "dns-target.us-east-2.awsapprunner.com"
REMOTE_REF = f"{ACCOUNT}.dkr.ecr.us-east-2.amazonaws.com/{NAME}:latest"
VALIDATION_RECORD = {
    "Name": "_abc.app.example.com",
    "Type": "CNAME",
    "Value": "_def.acm-validations.aws.",
}

STS_XML = f"""\
<GetCallerIdentityResponse xmlns="https://sts.amazonaws.com/doc/2011-06-15/">
  <GetCallerIdentityResult>
    <Arn>arn:aws:iam::{ACCOUNT}:user/homebox</Arn>
    <UserId>AIDAEXAMPLE</UserId>
    <Account>{ACCOUNT}</Account>
  </GetCallerIdentityResult>
</GetCallerIdentityResponse>""".encode()

ROLE_EXISTS_XML = b"""\
<ErrorResponse xmlns="https://iam.amazonaws.com/doc/2010-05-08/">
  <Error><Type>Sender</Type><Code>EntityAlreadyExists</Code>
  <Message>Role with name homebox-apprunner-ecr-access already exists.</Message>
  </Error><RequestId>req-1</RequestId>
</ErrorResponse>"""


def run(coro):
    return asyncio.run(coro)


def json_error(code: str, message: str, status: int = 400) -> httpx.Response:
    return httpx.Response(status, json={
        "__type": f"com.amazonaws.apprunner#{code}", "message": message,
    })


class FakeAws:
    """Routes MockTransport requests like IAM + STS + App Runner."""

    def __init__(self, *, existing: bool = False, statuses: list[str] | None = None,
                 current_ref: str = REMOTE_REF, role_exists: bool = False,
                 custom_domains: list[dict] | None = None,
                 associate_error: httpx.Response | None = None,
                 delete_response: httpx.Response | None = None,
                 sts_response: httpx.Response | None = None):
        self.existing = existing
        # DescribeService returns statuses front-to-back, holding the last.
        self.statuses = list(statuses or ["OPERATION_IN_PROGRESS", "RUNNING"])
        self.current_ref = current_ref
        self.role_exists = role_exists
        self.custom_domains = custom_domains or []
        self.associate_error = associate_error
        self.delete_response = delete_response
        self.sts_response = sts_response
        self.calls: list[tuple[str, dict]] = []
        self.headers: dict[str, dict[str, str]] = {}

    def _service(self) -> dict:
        status = self.statuses[0]
        if len(self.statuses) > 1:
            self.statuses.pop(0)
        return {
            "ServiceArn": ARN, "ServiceName": NAME, "Status": status,
            "ServiceUrl": SERVICE_URL,
            "SourceConfiguration": {
                "ImageRepository": {"ImageIdentifier": self.current_ref},
            },
        }

    def handler(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host
        if host == "sts.amazonaws.com":
            self.calls.append(("sts.GetCallerIdentity", {}))
            return self.sts_response or httpx.Response(200, content=STS_XML)
        if host == "iam.amazonaws.com":
            form = dict(urllib.parse.parse_qsl(request.content.decode()))
            self.calls.append((f"iam.{form['Action']}", form))
            if form["Action"] == "CreateRole":
                if self.role_exists:
                    return httpx.Response(409, content=ROLE_EXISTS_XML)
                self.role_exists = True
                return httpx.Response(200, content=b"<CreateRoleResponse/>")
            if form["Action"] == "AttachRolePolicy":
                return httpx.Response(200, content=b"<AttachRolePolicyResponse/>")
            raise AssertionError(f"unexpected IAM action: {form['Action']}")

        assert host == "apprunner.us-east-2.amazonaws.com", host
        op = request.headers["x-amz-target"].split(".")[-1]
        payload = json.loads(request.content)
        self.calls.append((op, payload))
        self.headers[op] = {
            "content-type": request.headers.get("content-type", ""),
            "x-amz-target": request.headers.get("x-amz-target", ""),
        }
        if op == "ListServices":
            summaries = ([{"ServiceName": NAME, "ServiceArn": ARN}]
                         if self.existing else [])
            return httpx.Response(200, json={"ServiceSummaryList": summaries})
        if op == "CreateService":
            self.existing = True
            return httpx.Response(200, json={"Service": {
                "ServiceArn": ARN, "ServiceName": NAME,
                "Status": "OPERATION_IN_PROGRESS", "ServiceUrl": SERVICE_URL,
            }})
        if op == "UpdateService":
            return httpx.Response(200, json={"Service": {"ServiceArn": ARN},
                                             "OperationId": "op-1"})
        if op == "StartDeployment":
            return httpx.Response(200, json={"OperationId": "op-2"})
        if op == "DescribeService":
            return httpx.Response(200, json={"Service": self._service()})
        if op == "DescribeCustomDomains":
            return httpx.Response(200, json={
                "DNSTarget": DNS_TARGET, "CustomDomains": self.custom_domains,
            })
        if op == "AssociateCustomDomain":
            if self.associate_error is not None:
                return self.associate_error
            return httpx.Response(200, json={
                "DNSTarget": DNS_TARGET,
                "CustomDomain": {
                    "DomainName": payload["DomainName"],
                    "CertificateValidationRecords": [VALIDATION_RECORD],
                },
            })
        if op == "DeleteService":
            return self.delete_response or httpx.Response(
                200, json={"Service": {"Status": "DELETING"}})
        raise AssertionError(f"unexpected App Runner op: {op}")

    def ops(self) -> list[str]:
        return [op for op, _ in self.calls]

    def payload(self, op: str) -> dict:
        return next(p for o, p in self.calls if o == op)


def target(fake: FakeAws, *, account_id: str | None = ACCOUNT) -> AppRunnerTarget:
    creds = {"key_id": "AKID", "secret": "sk", "region": "us-east-2"}
    if account_id:
        creds["account_id"] = account_id
    return AppRunnerTarget(
        creds, {}, {}, transport=httpx.MockTransport(fake.handler))


def make_ctx(hostname: str | None = None, **kwargs):
    lines: list[str] = []

    async def log(line: str) -> None:
        lines.append(line)

    ctx = TargetDeployCtx(
        project_name=kwargs.pop("project_name", "listless"),
        env_name="dev", service_name="web", kind="web",
        rd=Path("/nonexistent"), hostname=hostname,
        env_vars=kwargs.pop("env_vars", {"FOO": "bar", "DB_URL": "postgres://x"}),
        internal_port=kwargs.pop("internal_port", 3000),
        image=kwargs.pop("image", "homebox-listless-dev-web:build"),
        log=log, **kwargs,
    )
    return ctx, lines


@pytest.fixture(autouse=True)
def fast_poll(monkeypatch):
    monkeypatch.setattr(apprunner_mod, "_POLL_INTERVAL", 0.0)


@pytest.fixture()
def docker(monkeypatch):
    """Monkeypatch registry.ecr_push + the docker _run helper."""
    calls: dict = {"runs": [], "inspect_code": 0}

    async def fake_push(aws, image_name: str, local_tag: str) -> str:
        calls["image_name"] = image_name
        calls["local_tag"] = local_tag
        return REMOTE_REF

    async def fake_run(cmd, *, timeout=900, input_text=None):
        calls["runs"].append(cmd)
        if cmd[:3] == ["docker", "image", "inspect"]:
            return calls["inspect_code"], ""
        return 0, ""

    monkeypatch.setattr(registry, "ecr_push", fake_push)
    monkeypatch.setattr(registry, "_run", fake_run)
    return calls


# ───── helpers ────────────────────────────────────────────────────────────────


def test_service_name_sanitizes_and_truncates():
    ctx, _ = make_ctx(project_name="My App!")
    assert _service_name(ctx) == "homebox-My-App-dev-web"
    ctx, _ = make_ctx(project_name="x" * 60)
    name = _service_name(ctx)
    assert len(name) <= 40
    assert name.startswith("homebox-xxx")


# ───── validate ───────────────────────────────────────────────────────────────


def test_validate_ok_and_error():
    fake = FakeAws()
    run(target(fake).validate())
    assert fake.ops() == ["sts.GetCallerIdentity"]

    bad = FakeAws(sts_response=httpx.Response(403, content=(
        b"<ErrorResponse><Error><Code>InvalidClientTokenId</Code>"
        b"<Message>The security token is invalid</Message></Error></ErrorResponse>"
    )))
    with pytest.raises(TargetError) as exc:
        run(target(bad).validate())
    assert "InvalidClientTokenId" in str(exc.value)


# ───── fresh deploy ───────────────────────────────────────────────────────────


def test_fresh_deploy_full_flow(docker):
    fake = FakeAws()
    ctx, lines = make_ctx(hostname="app.example.com")
    result = run(target(fake).deploy(ctx))

    # IAM role bootstrap: trust policy for App Runner's build principal,
    # managed ECR-access policy attached.
    role = fake.payload("iam.CreateRole")
    assert role["RoleName"] == "homebox-apprunner-ecr-access"
    trust = json.loads(role["AssumeRolePolicyDocument"])
    assert trust["Statement"][0]["Principal"]["Service"] == \
        "build.apprunner.amazonaws.com"
    attach = fake.payload("iam.AttachRolePolicy")
    assert attach["PolicyArn"] == POLICY_ARN

    # Image pushed via ECR under the sanitized name.
    assert docker["image_name"] == NAME
    assert docker["local_tag"] == "homebox-listless-dev-web:build"

    # CreateService with the exact source configuration.
    create = fake.payload("CreateService")
    assert create["ServiceName"] == NAME
    src = create["SourceConfiguration"]
    assert src["ImageRepository"]["ImageIdentifier"] == REMOTE_REF
    assert src["ImageRepository"]["ImageRepositoryType"] == "ECR"
    img_cfg = src["ImageRepository"]["ImageConfiguration"]
    assert img_cfg["Port"] == "3000"  # string, not int
    assert img_cfg["RuntimeEnvironmentVariables"] == {
        "FOO": "bar", "DB_URL": "postgres://x"}
    assert src["AuthenticationConfiguration"]["AccessRoleArn"] == ROLE_ARN
    assert src["AutoDeploymentsEnabled"] is False
    assert create["InstanceConfiguration"] == {"Cpu": "1024", "Memory": "2048"}

    # x-amz-json-1.0 protocol headers.
    assert fake.headers["CreateService"]["content-type"] == \
        "application/x-amz-json-1.0"
    assert fake.headers["CreateService"]["x-amz-target"] == \
        "AppRunner.CreateService"

    # Polled through OPERATION_IN_PROGRESS to RUNNING.
    assert fake.ops().count("DescribeService") == 2

    # Custom domain associated; DNS handed back to the orchestrator.
    assoc = fake.payload("AssociateCustomDomain")
    assert assoc == {"ServiceArn": ARN, "DomainName": "app.example.com",
                     "EnableWWWSubdomain": False}
    assert result.endpoint == SERVICE_URL
    assert result.cname_target == DNS_TARGET
    assert result.proxied is False
    assert result.state["service_arn"] == ARN
    assert result.state["service_name"] == NAME
    assert result.state["url"] == SERVICE_URL
    assert result.state["extra_dns_records"] == [{
        "name": VALIDATION_RECORD["Name"],
        "type": "CNAME",
        "value": VALIDATION_RECORD["Value"],
    }]
    assert not fake.payload("ListServices")  # no filters needed
    assert any("creating App Runner service" in ln for ln in lines)


def test_rerun_tolerates_existing_role(docker):
    fake = FakeAws(role_exists=True)  # CreateRole → 409 EntityAlreadyExists
    ctx, _ = make_ctx()
    result = run(target(fake).deploy(ctx))
    assert "iam.AttachRolePolicy" in fake.ops()  # still (re-)attached
    assert result.state["service_arn"] == ARN


def test_no_hostname_skips_domain_and_cname(docker):
    fake = FakeAws()
    ctx, _ = make_ctx(hostname=None)
    result = run(target(fake).deploy(ctx))
    assert "DescribeCustomDomains" not in fake.ops()
    assert "AssociateCustomDomain" not in fake.ops()
    assert result.cname_target is None
    assert result.proxied is False
    assert "extra_dns_records" not in result.state


def test_account_id_falls_back_to_sts(docker):
    fake = FakeAws()
    ctx, _ = make_ctx()
    run(target(fake, account_id=None).deploy(ctx))
    assert "sts.GetCallerIdentity" in fake.ops()
    create = fake.payload("CreateService")
    assert create["SourceConfiguration"]["AuthenticationConfiguration"] == \
        {"AccessRoleArn": ROLE_ARN}


def test_upstream_image_pulled_when_missing_locally(docker):
    docker["inspect_code"] = 1  # not present locally
    fake = FakeAws()
    ctx, _ = make_ctx(image="postgres:16")
    run(target(fake).deploy(ctx))
    assert ["docker", "pull", "postgres:16"] in docker["runs"]
    assert docker["local_tag"] == "postgres:16"


# ───── update path ────────────────────────────────────────────────────────────


def test_update_unchanged_ref_fires_start_deployment(docker):
    fake = FakeAws(existing=True, current_ref=REMOTE_REF,
                   statuses=["RUNNING", "OPERATION_IN_PROGRESS", "RUNNING"])
    ctx, _ = make_ctx()
    result = run(target(fake).deploy(ctx))
    assert "CreateService" not in fake.ops()
    update = fake.payload("UpdateService")
    assert update["ServiceArn"] == ARN
    assert update["SourceConfiguration"]["ImageRepository"]["ImageIdentifier"] \
        == REMOTE_REF
    # Same :latest ref → explicit re-pull.
    assert fake.payload("StartDeployment") == {"ServiceArn": ARN}
    assert result.endpoint == SERVICE_URL


def test_update_changed_ref_skips_start_deployment(docker):
    fake = FakeAws(existing=True, current_ref="old.example/other:latest",
                   statuses=["RUNNING", "OPERATION_IN_PROGRESS", "RUNNING"])
    ctx, _ = make_ctx()
    run(target(fake).deploy(ctx))
    assert "UpdateService" in fake.ops()
    assert "StartDeployment" not in fake.ops()


def test_domain_already_associated_is_idempotent(docker):
    fake = FakeAws(custom_domains=[{
        "DomainName": "app.example.com",
        "CertificateValidationRecords": [VALIDATION_RECORD],
    }])
    ctx, _ = make_ctx(hostname="app.example.com")
    result = run(target(fake).deploy(ctx))
    assert "AssociateCustomDomain" not in fake.ops()
    assert result.cname_target == DNS_TARGET
    assert result.state["extra_dns_records"][0]["name"] == \
        VALIDATION_RECORD["Name"]


def test_domain_association_race_tolerated(docker):
    fake = FakeAws(associate_error=json_error(
        "InvalidStateException",
        "Domain app.example.com is already associated with the service"))
    ctx, _ = make_ctx(hostname="app.example.com")
    result = run(target(fake).deploy(ctx))
    assert result.cname_target == DNS_TARGET
    assert result.state["extra_dns_records"] == []


# ───── failure paths ──────────────────────────────────────────────────────────


def test_create_failed_status_raises(docker):
    fake = FakeAws(statuses=["OPERATION_IN_PROGRESS", "CREATE_FAILED"])
    ctx, _ = make_ctx()
    with pytest.raises(TargetError) as exc:
        run(target(fake).deploy(ctx))
    assert "CREATE_FAILED" in str(exc.value)


def test_deploy_wraps_aws_errors(docker):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, content=(
            b"<ErrorResponse><Error><Code>AccessDenied</Code>"
            b"<Message>not authorized to perform iam:CreateRole</Message>"
            b"</Error></ErrorResponse>"))

    t = AppRunnerTarget({"key_id": "k", "secret": "s", "region": "us-east-2",
                         "account_id": ACCOUNT}, {}, {},
                        transport=httpx.MockTransport(handler))
    ctx, _ = make_ctx()
    with pytest.raises(TargetError) as exc:
        run(t.deploy(ctx))
    assert "AccessDenied" in str(exc.value)


# ───── destroy / probe ────────────────────────────────────────────────────────


def test_destroy_deletes_service():
    fake = FakeAws()
    run(target(fake).destroy({"service_arn": ARN}))
    assert fake.payload("DeleteService") == {"ServiceArn": ARN}


def test_destroy_tolerates_missing_and_empty_state():
    fake = FakeAws(delete_response=json_error(
        "ResourceNotFoundException", "Service not found", status=400))
    run(target(fake).destroy({"service_arn": ARN}))  # no raise

    deleting = FakeAws(delete_response=json_error(
        "InvalidStateException", "Service is already being deleted"))
    run(target(deleting).destroy({"service_arn": ARN}))  # no raise

    untouched = FakeAws()
    run(target(untouched).destroy({}))
    run(target(untouched).destroy(None))
    assert untouched.calls == []


def test_destroy_raises_on_other_errors():
    fake = FakeAws(delete_response=json_error(
        "InternalServiceErrorException", "boom", status=500))
    with pytest.raises(TargetError):
        run(target(fake).destroy({"service_arn": ARN}))


def test_probe_running_true():
    fake = FakeAws(statuses=["RUNNING"])
    assert run(target(fake).probe({"service_arn": ARN})) is True


def test_probe_false_paths():
    in_progress = FakeAws(statuses=["OPERATION_IN_PROGRESS"])
    assert run(target(in_progress).probe({"service_arn": ARN})) is False

    def not_found(request: httpx.Request) -> httpx.Response:
        return json_error("ResourceNotFoundException", "no such service")

    t = AppRunnerTarget({"key_id": "k", "secret": "s", "region": "us-east-2"},
                        {}, {}, transport=httpx.MockTransport(not_found))
    assert run(t.probe({"service_arn": ARN})) is False
    assert run(t.probe({})) is False
