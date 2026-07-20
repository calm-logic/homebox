"""Tests for the GCP Cloud Run target (app/targets/gcp_cloud_run.py).

All Cloud Run Admin API v2 behavior — create-or-update (GET → 404 → POST,
else PATCH), long-running-operation polling, setIamPolicy public access,
endpoint from the service URI, destroy, probe — plus the custom-domain flow
(Google Site Verification via DNS TXT and v1 Knative domain mappings) runs
against httpx.MockTransport. registry.artifact_registry_push and the docker
`_run` helper are monkeypatched, so no docker daemon and no network are
touched.
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.targets import gcp_cloud_run  # noqa: E402
from app.targets.base import TargetDeployCtx, TargetError  # noqa: E402
from app.targets.gcp_cloud_run import CloudRunTarget, _service_id  # noqa: E402

PROJECT = "proj-1"
REGION = "us-central1"
SVC = "homebox-listless-dev-api"
PARENT = f"/v2/projects/{PROJECT}/locations/{REGION}"
SVC_PATH = f"{PARENT}/services/{SVC}"
OP_NAME = f"projects/{PROJECT}/locations/{REGION}/operations/op-1"
OP_PATH = f"/v2/{OP_NAME}"
URI = "https://homebox-listless-dev-api-abc123-uc.a.run.app"
REMOTE_REF = f"{REGION}-docker.pkg.dev/{PROJECT}/homebox/{SVC}:latest"

# Custom-domain fixtures (site verification + v1 Knative domain mappings).
HOSTNAME = "api.listless.example.com"
MAP_BASE = f"/apis/domains.cloudrun.com/v1/namespaces/{PROJECT}/domainmappings"
GHS = "ghs.googlehosted.com."          # rrdata as Google returns it
GHS_CLEAN = "ghs.googlehosted.com"     # …and after the target strips the dot
TXT_TOKEN = "google-site-verification-token-1"


def run(coro):
    return asyncio.run(coro)


def _404() -> httpx.Response:
    return httpx.Response(404, json={
        "error": {"code": 404, "message": "Resource not found",
                  "status": "NOT_FOUND"},
    })


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
        "project_id": PROJECT,
        "client_email": f"deployer@{PROJECT}.iam.gserviceaccount.com",
        "private_key": pem,
    }


@pytest.fixture(autouse=True)
def no_docker_no_sleep(monkeypatch):
    """Zero the operation poll interval and stub out everything that would
    touch docker: the module's `_run` (docker pull) and
    registry.artifact_registry_push (repo create + login + tag + push)."""
    monkeypatch.setattr(gcp_cloud_run, "OP_POLL_INTERVAL", 0)
    monkeypatch.setattr(gcp_cloud_run, "MAPPING_POLL_INTERVAL", 0)

    stub = SimpleNamespace(pushes=[], docker_calls=[], pull_rc=0)

    async def fake_push(gcp, region, image_name, local_tag):
        stub.pushes.append({"project": gcp.project_id, "region": region,
                            "image_name": image_name, "local_tag": local_tag})
        return f"{region}-docker.pkg.dev/{gcp.project_id}/homebox/{image_name}:latest"

    async def fake_run(cmd, *, timeout=900, input_text=None):
        stub.docker_calls.append(cmd)
        return (stub.pull_rc, "boom" if stub.pull_rc else "")

    monkeypatch.setattr(gcp_cloud_run.registry, "artifact_registry_push", fake_push)
    monkeypatch.setattr(gcp_cloud_run, "_run", fake_run)
    return stub


class FakeCloudRun:
    """Routes MockTransport requests like the Cloud Run Admin API v2 (and
    answers the OAuth token endpoint, the Site Verification API and the v1
    Knative domain-mappings surface itself)."""

    def __init__(self, *, exists: bool = False, op_polls: int = 1,
                 op_never_done: bool = False, healthy: bool = True,
                 verify_ok: bool = True, mapping_exists: bool = False,
                 mapping_records: bool = True):
        self.exists = exists
        self.op_polls = op_polls          # op GETs before done=True
        self.op_never_done = op_never_done
        self.healthy = healthy
        self.requests: list[tuple[str, str]] = []
        self.create_body: dict | None = None
        self.create_params: dict | None = None
        self.patch_body: dict | None = None
        self.iam_body: dict | None = None
        self.op_gets = 0
        # custom-domain knobs / captures
        self.verify_ok = verify_ok            # webResource insert succeeds
        self.mapping_exists = mapping_exists
        self.mapping_records = mapping_records  # GETs carry resourceRecords
        self.token_body: dict | None = None
        self.verify_bodies: list[dict] = []
        self.verify_params: list[dict] = []
        self.map_create_body: dict | None = None
        self.map_gets = 0

    def _service(self) -> dict:
        svc = {"name": SVC_PATH.removeprefix("/v2/"), "uri": URI}
        if self.healthy:
            svc["terminalCondition"] = {"state": "CONDITION_SUCCEEDED"}
            svc["latestReadyRevision"] = f"{SVC_PATH.removeprefix('/v2/')}/revisions/{SVC}-00001-abc"
        return svc

    def _mapping(self) -> dict:
        m = {"apiVersion": "domains.cloudrun.com/v1", "kind": "DomainMapping",
             "metadata": {"name": HOSTNAME, "namespace": PROJECT},
             "spec": {"routeName": SVC, "certificateMode": "AUTOMATIC"}}
        if self.mapping_records:
            m["status"] = {"resourceRecords": [
                {"name": "api", "type": "CNAME", "rrdata": GHS}]}
        return m

    def _siteverification(self, request: httpx.Request) -> httpx.Response:
        method, path = request.method, request.url.path
        if method == "POST" and path == "/siteVerification/v1/token":
            self.token_body = json.loads(request.content)
            return httpx.Response(200, json={"token": TXT_TOKEN})
        if method == "POST" and path == "/siteVerification/v1/webResource":
            self.verify_params.append(dict(request.url.params))
            self.verify_bodies.append(json.loads(request.content))
            if self.verify_ok:
                return httpx.Response(200, json={
                    "id": f"dns://{HOSTNAME}/",
                    "site": {"type": "INET_DOMAIN", "identifier": HOSTNAME},
                })
            return httpx.Response(400, json={"error": {
                "code": 400,
                "message": "Verification token not found in DNS TXT records.",
                "status": "INVALID_ARGUMENT",
            }})
        raise AssertionError(f"unexpected siteVerification: {method} {path}")

    def _domainmappings(self, request: httpx.Request) -> httpx.Response:
        method, path = request.method, request.url.path
        one = f"{MAP_BASE}/{HOSTNAME}"
        if method == "GET" and path == one:
            self.map_gets += 1
            if not self.mapping_exists:
                return _404()
            return httpx.Response(200, json=self._mapping())
        if method == "POST" and path == MAP_BASE:
            self.map_create_body = json.loads(request.content)
            self.mapping_exists = True
            # A freshly-created mapping has no resourceRecords yet.
            m = self._mapping()
            m.pop("status", None)
            return httpx.Response(200, json=m)
        if method == "DELETE" and path == one:
            if not self.mapping_exists:
                return _404()
            self.mapping_exists = False
            return httpx.Response(200, json={})
        raise AssertionError(f"unexpected domain-mapping: {method} {path}")

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.host == "oauth2.googleapis.com":
            return httpx.Response(200, json={"access_token": "tok",
                                             "expires_in": 3600})
        if request.url.host == "www.googleapis.com":
            self.requests.append((request.method, request.url.path))
            return self._siteverification(request)
        if request.url.host == f"{REGION}-run.googleapis.com":
            self.requests.append((request.method, request.url.path))
            return self._domainmappings(request)
        assert request.url.host == "run.googleapis.com"
        method, path = request.method, request.url.path
        self.requests.append((method, path))
        if method == "GET" and path == SVC_PATH:
            return httpx.Response(200, json=self._service()) if self.exists else _404()
        if method == "POST" and path == f"{PARENT}/services":
            self.create_params = dict(request.url.params)
            self.create_body = json.loads(request.content)
            self.exists = True
            return httpx.Response(200, json={"name": OP_NAME, "done": False})
        if method == "PATCH" and path == SVC_PATH:
            self.patch_body = json.loads(request.content)
            return httpx.Response(200, json={"name": OP_NAME, "done": False})
        if method == "GET" and path == OP_PATH:
            self.op_gets += 1
            done = (not self.op_never_done) and self.op_gets >= self.op_polls
            return httpx.Response(200, json={"name": OP_NAME, "done": done})
        if method == "POST" and path == f"{SVC_PATH}:setIamPolicy":
            self.iam_body = json.loads(request.content)
            return httpx.Response(200, json={"bindings": []})
        if method == "DELETE" and path == SVC_PATH:
            if not self.exists:
                return _404()
            self.exists = False
            return httpx.Response(200, json={"name": OP_NAME, "done": False})
        raise AssertionError(f"unexpected request: {method} {path}")


def target(fake: FakeCloudRun, sa: dict, config: dict | None = None,
           state: dict | None = None) -> CloudRunTarget:
    return CloudRunTarget(
        creds={"sa": sa}, config=config or {}, state=state or {},
        transport=httpx.MockTransport(fake.handler),
    )


# hostname defaults to None: the pre-domain-mapping tests exercise the pure
# service-deploy mechanics (create/patch/op-poll/IAM/URI), which for
# hostname=None are byte-for-byte today's behavior. Domain-mapping tests opt
# in with hostname=HOSTNAME.
def make_ctx(env_vars: dict | None = None, image: str = "listless-api:build-7",
             port: int | None = 8000, hostname: str | None = None):
    lines: list[str] = []

    async def log(line: str) -> None:
        lines.append(line)

    ctx = TargetDeployCtx(
        project_name="listless", env_name="dev", service_name="api",
        kind="api", rd=Path("/nonexistent"),
        hostname=hostname,
        env_vars=env_vars if env_vars is not None else {},
        internal_port=port, image=image, log=log,
    )
    return ctx, lines


# ───── deploy: fresh service ──────────────────────────────────────────────────


def test_fresh_deploy_creates_service(sa, no_docker_no_sleep):
    fake = FakeCloudRun(exists=False)
    ctx, lines = make_ctx(env_vars={"DATABASE_URL": "postgres://db"})
    result = run(target(fake, sa).deploy(ctx))

    # Image was pulled (best-effort) and pushed to Artifact Registry under
    # the sanitized service id.
    assert ["docker", "pull", "listless-api:build-7"] in no_docker_no_sleep.docker_calls
    assert no_docker_no_sleep.pushes == [{
        "project": PROJECT, "region": REGION,
        "image_name": SVC, "local_tag": "listless-api:build-7",
    }]

    # GET 404 → POST create → op polled → setIamPolicy → final GET for uri.
    assert fake.requests[0] == ("GET", SVC_PATH)
    assert fake.requests[1] == ("POST", f"{PARENT}/services")
    assert ("GET", OP_PATH) in fake.requests
    assert ("POST", f"{SVC_PATH}:setIamPolicy") in fake.requests
    assert fake.requests[-1] == ("GET", SVC_PATH)
    assert not any(m == "PATCH" for m, _ in fake.requests)

    # Create carries the deterministic service id and the full body.
    assert fake.create_params == {"serviceId": SVC}
    container = fake.create_body["template"]["containers"][0]
    assert container["image"] == REMOTE_REF
    assert container["ports"] == [{"containerPort": 8000}]
    assert container["env"] == [{"name": "DATABASE_URL", "value": "postgres://db"}]
    assert fake.create_body["template"]["scaling"] == {
        "minInstanceCount": 0, "maxInstanceCount": 3,
    }
    assert fake.create_body["ingress"] == "INGRESS_TRAFFIC_ALL"

    # Public access binding.
    assert fake.iam_body == {"policy": {"bindings": [
        {"role": "roles/run.invoker", "members": ["allUsers"]},
    ]}}

    # Result contract: run.app host, NO DNS record (cname_target=None).
    assert result.endpoint == URI.removeprefix("https://")
    assert result.cname_target is None
    assert result.proxied is True
    assert result.state == {
        "service_id": SVC, "region": REGION, "project": PROJECT,
        "uri": URI, "image": REMOTE_REF,
    }
    assert any("creating Cloud Run service" in line for line in lines)
    assert any("pushing image" in line for line in lines)


def test_default_port_is_8080(sa):
    fake = FakeCloudRun(exists=False)
    ctx, _ = make_ctx(port=None)
    run(target(fake, sa).deploy(ctx))
    container = fake.create_body["template"]["containers"][0]
    assert container["ports"] == [{"containerPort": 8080}]


def test_deploy_tolerates_pull_failure_for_local_only_tag(sa, no_docker_no_sleep):
    # Locally-built tags aren't pullable — the pull failure must not abort
    # the deploy (the tag/push inside artifact_registry_push handles it).
    no_docker_no_sleep.pull_rc = 1
    fake = FakeCloudRun(exists=False)
    ctx, lines = make_ctx()
    result = run(target(fake, sa).deploy(ctx))
    assert result.state["image"] == REMOTE_REF
    assert any("docker pull" in line for line in lines)


# ───── deploy: existing service ───────────────────────────────────────────────


def test_second_deploy_patches_existing_service(sa):
    fake = FakeCloudRun(exists=True)
    ctx, lines = make_ctx(image="listless-api:build-8")
    result = run(target(fake, sa).deploy(ctx))

    # GET 200 → PATCH; no create POST anywhere.
    assert fake.requests[0] == ("GET", SVC_PATH)
    assert fake.requests[1] == ("PATCH", SVC_PATH)
    assert ("POST", f"{PARENT}/services") not in fake.requests
    assert fake.create_body is None

    # Image updated in the PATCH body.
    assert fake.patch_body["template"]["containers"][0]["image"] == REMOTE_REF
    assert result.state["service_id"] == SVC
    assert any("updating Cloud Run service" in line for line in lines)


def test_env_vars_become_name_value_list(sa):
    fake = FakeCloudRun(exists=True)
    ctx, _ = make_ctx(env_vars={"A": "1", "B": "two"})
    run(target(fake, sa).deploy(ctx))
    env = fake.patch_body["template"]["containers"][0]["env"]
    assert sorted(env, key=lambda e: e["name"]) == [
        {"name": "A", "value": "1"},
        {"name": "B", "value": "two"},
    ]


# ───── operation polling ──────────────────────────────────────────────────────


def test_operation_polled_until_done(sa):
    fake = FakeCloudRun(exists=False, op_polls=3)
    ctx, _ = make_ctx()
    run(target(fake, sa).deploy(ctx))
    assert fake.op_gets == 3


def test_operation_never_done_raises_after_cap(sa):
    fake = FakeCloudRun(exists=False, op_never_done=True)
    ctx, _ = make_ctx()
    with pytest.raises(TargetError) as exc:
        run(target(fake, sa).deploy(ctx))
    assert "did not complete" in str(exc.value)
    assert fake.op_gets == gcp_cloud_run.OP_POLL_ATTEMPTS
    # The deploy stopped there — public access was never granted.
    assert fake.iam_body is None


def test_operation_error_surfaces_as_target_error(sa):
    fake = FakeCloudRun(exists=False)
    real = fake.handler

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == OP_PATH:
            return httpx.Response(200, json={
                "name": OP_NAME, "done": True,
                "error": {"code": 9, "message": "Revision failed to start"},
            })
        return real(request)

    ctx, _ = make_ctx()
    t = CloudRunTarget(creds={"sa": sa}, config={}, state={},
                       transport=httpx.MockTransport(handler))
    with pytest.raises(TargetError) as exc:
        run(t.deploy(ctx))
    assert "Revision failed to start" in str(exc.value)


# ───── region config ──────────────────────────────────────────────────────────


def test_region_from_config(sa):
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "oauth2.googleapis.com":
            return httpx.Response(200, json={"access_token": "tok",
                                             "expires_in": 3600})
        seen.append(request.url.path)
        return _404()

    t = CloudRunTarget(creds={"sa": sa}, config={"region": "europe-west1"},
                       state={}, transport=httpx.MockTransport(handler))
    assert run(t.probe({"service_id": SVC})) is False
    assert seen == [f"/v2/projects/{PROJECT}/locations/europe-west1/services/{SVC}"]


# ───── validate ───────────────────────────────────────────────────────────────


def test_validate_ok(sa):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "oauth2.googleapis.com":
            return httpx.Response(200, json={"access_token": "tok",
                                             "expires_in": 3600})
        assert request.url.path == f"/v1/projects/{PROJECT}"
        return httpx.Response(200, json={"projectId": PROJECT})

    t = CloudRunTarget(creds={"sa": sa}, config={}, state={},
                       transport=httpx.MockTransport(handler))
    run(t.validate())  # no raise


def test_validate_permission_denied_is_actionable(sa):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "oauth2.googleapis.com":
            return httpx.Response(200, json={"access_token": "tok",
                                             "expires_in": 3600})
        return httpx.Response(403, json={"error": {
            "code": 403, "message": "The caller does not have permission",
            "status": "PERMISSION_DENIED",
        }})

    t = CloudRunTarget(creds={"sa": sa}, config={}, state={},
                       transport=httpx.MockTransport(handler))
    with pytest.raises(TargetError) as exc:
        run(t.validate())
    assert "Cloud Run Admin" in str(exc.value)


def test_missing_sa_raises_target_error():
    t = CloudRunTarget(creds={}, config={}, state={})
    with pytest.raises(TargetError) as exc:
        run(t.validate())
    assert "service-account" in str(exc.value)


# ───── custom domain: site verification + domain mapping ─────────────────────


def test_hostname_pending_verification_falls_back_to_run_app(sa):
    # DNS TXT not propagated yet: deploy still SUCCEEDS, hands the TXT
    # record to the orchestrator, and keeps the run.app fallback.
    fake = FakeCloudRun(exists=False, verify_ok=False)
    ctx, lines = make_ctx(hostname=HOSTNAME)
    result = run(target(fake, sa).deploy(ctx))

    assert result.endpoint == URI.removeprefix("https://")
    assert result.cname_target is None
    assert result.proxied is True
    assert result.state["domain_mapping"] == "pending_verification"
    assert result.state["hostname"] == HOSTNAME
    assert result.state["extra_dns_records"] == [
        {"type": "TXT", "name": HOSTNAME, "value": TXT_TOKEN},
    ]
    # Verification request shapes.
    assert fake.token_body == {
        "site": {"type": "INET_DOMAIN", "identifier": HOSTNAME},
        "verificationMethod": "DNS_TXT",
    }
    assert fake.verify_params == [{"verificationMethod": "DNS_TXT"}]
    assert fake.verify_bodies == [
        {"site": {"type": "INET_DOMAIN", "identifier": HOSTNAME}},
    ]
    # No mapping calls while unverified.
    assert not any("/domainmappings" in p for _, p in fake.requests)
    assert any("not verified" in line for line in lines)


def test_verified_creates_mapping_and_returns_cname(sa):
    fake = FakeCloudRun(exists=False)
    ctx, lines = make_ctx(hostname=HOSTNAME)
    result = run(target(fake, sa).deploy(ctx))

    # v1 Knative namespaces path + full DomainMapping body.
    assert ("GET", f"{MAP_BASE}/{HOSTNAME}") in fake.requests   # 404 first
    assert ("POST", MAP_BASE) in fake.requests
    assert fake.map_create_body == {
        "apiVersion": "domains.cloudrun.com/v1",
        "kind": "DomainMapping",
        "metadata": {"name": HOSTNAME, "namespace": PROJECT},
        "spec": {"routeName": SVC, "certificateMode": "AUTOMATIC"},
    }

    assert result.cname_target == GHS_CLEAN   # trailing dot stripped
    assert result.proxied is False            # grey cloud: Google cert provisioning
    assert result.state["domain_mapping"] == "ready"
    assert result.state["mapping_cname"] == GHS_CLEAN
    # The TXT record is still emitted (this deploy started unverified).
    assert result.state["extra_dns_records"] == [
        {"type": "TXT", "name": HOSTNAME, "value": TXT_TOKEN},
    ]
    assert any("domain mapping ready" in line for line in lines)


def test_existing_mapping_is_not_recreated(sa):
    fake = FakeCloudRun(exists=True, mapping_exists=True)
    ctx, _ = make_ctx(hostname=HOSTNAME)
    result = run(target(fake, sa).deploy(ctx))

    assert ("POST", MAP_BASE) not in fake.requests
    assert fake.map_create_body is None
    assert fake.map_gets == 1  # one GET was enough (records already present)
    assert result.cname_target == GHS_CLEAN
    assert result.state["domain_mapping"] == "ready"


def test_ready_state_short_circuits_without_reverifying(sa):
    fake = FakeCloudRun(exists=True, mapping_exists=True)
    prior = {"service_id": SVC, "domain_mapping": "ready",
             "mapping_cname": GHS_CLEAN, "hostname": HOSTNAME}
    ctx, _ = make_ctx(hostname=HOSTNAME)
    result = run(target(fake, sa, state=prior).deploy(ctx))

    assert result.cname_target == GHS_CLEAN
    assert result.proxied is False
    assert result.state["domain_mapping"] == "ready"
    # One existence GET, no re-verification, no POST, no new TXT record.
    assert fake.map_gets == 1
    assert fake.map_create_body is None
    assert not any(p.startswith("/siteVerification") for _, p in fake.requests)
    assert "extra_dns_records" not in result.state


def test_ready_state_recreates_vanished_mapping(sa):
    # state says ready but the mapping is gone (GET 404) → recreate it
    # without re-verifying (the domain verification persists in Google).
    fake = FakeCloudRun(exists=True, mapping_exists=False)
    prior = {"service_id": SVC, "domain_mapping": "ready",
             "mapping_cname": GHS_CLEAN, "hostname": HOSTNAME}
    ctx, _ = make_ctx(hostname=HOSTNAME)
    result = run(target(fake, sa, state=prior).deploy(ctx))

    assert not any(p.startswith("/siteVerification") for _, p in fake.requests)
    assert fake.map_create_body is not None
    assert result.cname_target == GHS_CLEAN
    assert result.state["domain_mapping"] == "ready"


def test_pending_verification_state_retries_and_completes(sa):
    # Reconcile-loop retry: prior deploy left pending_verification; the TXT
    # has propagated now → verify, map, ready.
    fake = FakeCloudRun(exists=True, verify_ok=True)
    prior = {"service_id": SVC, "domain_mapping": "pending_verification",
             "hostname": HOSTNAME}
    ctx, _ = make_ctx(hostname=HOSTNAME)
    result = run(target(fake, sa, state=prior).deploy(ctx))

    assert fake.verify_bodies  # re-verified
    assert result.cname_target == GHS_CLEAN
    assert result.state["domain_mapping"] == "ready"


def test_pending_mapping_state_skips_reverification(sa):
    # Verification already succeeded last cycle; only the mapping was slow.
    fake = FakeCloudRun(exists=True, mapping_exists=True)
    prior = {"service_id": SVC, "domain_mapping": "pending_mapping",
             "hostname": HOSTNAME}
    ctx, _ = make_ctx(hostname=HOSTNAME)
    result = run(target(fake, sa, state=prior).deploy(ctx))

    assert not any(p.startswith("/siteVerification") for _, p in fake.requests)
    assert result.cname_target == GHS_CLEAN
    assert result.state["domain_mapping"] == "ready"


def test_mapping_stays_pending_falls_back(sa, monkeypatch):
    monkeypatch.setattr(gcp_cloud_run, "MAPPING_POLL_ATTEMPTS", 2)
    fake = FakeCloudRun(exists=False, mapping_records=False)
    ctx, lines = make_ctx(hostname=HOSTNAME)
    result = run(target(fake, sa).deploy(ctx))

    assert result.cname_target is None
    assert result.proxied is True
    assert result.state["domain_mapping"] == "pending_mapping"
    assert any("still provisioning" in line for line in lines)


def test_domain_mapping_config_off_keeps_today_behavior(sa):
    fake = FakeCloudRun(exists=False)
    ctx, _ = make_ctx(hostname=HOSTNAME)
    result = run(target(fake, sa, config={"domain_mapping": False}).deploy(ctx))

    assert result.cname_target is None
    assert result.proxied is True
    assert set(result.state) == {"service_id", "region", "project", "uri",
                                 "image"}
    assert not any(
        p.startswith("/siteVerification") or "/domainmappings" in p
        for _, p in fake.requests
    )


def test_no_hostname_makes_no_domain_calls(sa):
    fake = FakeCloudRun(exists=False)
    ctx, _ = make_ctx()  # hostname=None
    result = run(target(fake, sa).deploy(ctx))
    assert result.cname_target is None
    assert "domain_mapping" not in result.state
    assert not any(
        p.startswith("/siteVerification") or "/domainmappings" in p
        for _, p in fake.requests
    )


def test_siteverification_403_is_actionable(sa):
    fake = FakeCloudRun(exists=False)
    real = fake.handler

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "www.googleapis.com":
            return httpx.Response(403, json={"error": {
                "code": 403,
                "message": "Site Verification API has not been used in "
                           "project proj-1 before or it is disabled.",
                "status": "PERMISSION_DENIED",
            }})
        return real(request)

    ctx, _ = make_ctx(hostname=HOSTNAME)
    t = CloudRunTarget(creds={"sa": sa}, config={}, state={},
                       transport=httpx.MockTransport(handler))
    with pytest.raises(TargetError) as exc:
        run(t.deploy(ctx))
    assert "siteverification.googleapis.com" in str(exc.value)


# ───── destroy ────────────────────────────────────────────────────────────────


def test_destroy_deletes_and_polls_op(sa):
    fake = FakeCloudRun(exists=True)
    run(target(fake, sa).destroy({"service_id": SVC, "region": REGION,
                                  "project": PROJECT}))
    assert ("DELETE", SVC_PATH) in fake.requests
    assert fake.op_gets >= 1
    assert fake.exists is False


def test_destroy_tolerates_404(sa):
    fake = FakeCloudRun(exists=False)
    run(target(fake, sa).destroy({"service_id": SVC}))  # no raise
    assert fake.requests == [("DELETE", SVC_PATH)]


def test_destroy_deletes_domain_mapping_then_service(sa):
    fake = FakeCloudRun(exists=True, mapping_exists=True)
    run(target(fake, sa).destroy({
        "service_id": SVC, "region": REGION, "project": PROJECT,
        "hostname": HOSTNAME, "domain_mapping": "ready",
        "mapping_cname": GHS_CLEAN,
    }))
    assert ("DELETE", f"{MAP_BASE}/{HOSTNAME}") in fake.requests
    assert fake.mapping_exists is False
    assert ("DELETE", SVC_PATH) in fake.requests
    assert fake.exists is False


def test_destroy_tolerates_missing_domain_mapping(sa):
    # Mapping already gone (DELETE → 404): not an error, service still removed.
    fake = FakeCloudRun(exists=True, mapping_exists=False)
    run(target(fake, sa).destroy({
        "service_id": SVC, "hostname": HOSTNAME, "domain_mapping": "ready",
    }))
    assert ("DELETE", f"{MAP_BASE}/{HOSTNAME}") in fake.requests
    assert fake.exists is False


def test_destroy_skips_mapping_never_created(sa):
    # pending_verification never created a mapping — no delete attempted.
    fake = FakeCloudRun(exists=True)
    run(target(fake, sa).destroy({
        "service_id": SVC, "hostname": HOSTNAME,
        "domain_mapping": "pending_verification",
    }))
    assert not any("/domainmappings" in p for _, p in fake.requests)
    assert fake.exists is False


def test_destroy_without_state_is_a_noop(sa):
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no request expected")

    t = CloudRunTarget(creds={"sa": sa}, config={}, state={},
                       transport=httpx.MockTransport(handler))
    run(t.destroy({}))
    run(t.destroy(None))


# ───── probe ──────────────────────────────────────────────────────────────────


def test_probe_true_when_service_ready(sa):
    fake = FakeCloudRun(exists=True, healthy=True)
    assert run(target(fake, sa).probe({"service_id": SVC})) is True


def test_probe_false_when_service_gone(sa):
    fake = FakeCloudRun(exists=False)
    assert run(target(fake, sa).probe({"service_id": SVC})) is False


def test_probe_false_when_service_not_ready(sa):
    fake = FakeCloudRun(exists=True, healthy=False)
    assert run(target(fake, sa).probe({"service_id": SVC})) is False


def test_probe_false_without_state(sa):
    fake = FakeCloudRun(exists=True)
    assert run(target(fake, sa).probe({})) is False


# ───── service-id sanitizer ───────────────────────────────────────────────────


def id_ctx(project: str, env: str = "dev", service: str = "api") -> TargetDeployCtx:
    return TargetDeployCtx(project_name=project, env_name=env,
                           service_name=service, kind="api",
                           rd=Path("/nonexistent"), hostname=None)


def test_service_id_passthrough():
    assert _service_id(id_ctx("listless")) == SVC


def test_service_id_lowercases_and_strips_invalid_chars():
    assert _service_id(id_ctx("My_App!! (v2)")) == "homebox-my-app-v2-dev-api"


def test_service_id_truncates_to_63_without_trailing_dash():
    n = _service_id(id_ctx("x" * 80))
    assert len(n) == 63
    assert n == "homebox-" + "x" * 55
    # Truncation landing exactly on a dash must not leave one trailing.
    n = _service_id(id_ctx("x" * 54 + "-tail"))
    assert len(n) <= 63
    assert not n.endswith("-")


def test_service_id_never_starts_with_digit_or_dash():
    # resource_name normally starts with "homebox-", but the sanitizer must
    # hold for arbitrary inputs too.
    assert _service_id(SimpleNamespace(resource_name="9lives")) == "s-9lives"
    assert _service_id(SimpleNamespace(resource_name="-dashy-")) == "dashy"
    assert _service_id(SimpleNamespace(resource_name="___")) == "s"


def test_service_id_always_valid():
    import re as _re
    for raw in ("UPPER case", "dots.and.slashes/here", "a" * 200, "-weird-"):
        n = _service_id(id_ctx(raw))
        assert 0 < len(n) <= 63
        assert _re.fullmatch(r"[a-z]([a-z0-9-]*[a-z0-9])?", n), n
        assert "--" not in n
