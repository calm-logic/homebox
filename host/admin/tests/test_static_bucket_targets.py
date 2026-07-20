"""Tests for the static bucket targets (app/targets/aws_s3.py + gcp_gcs.py).

Static services deploy as hostname-named public buckets behind proxied
Cloudflare CNAMEs. Both providers are exercised end-to-end against
httpx.MockTransport fakes that route like the real APIs — S3's XML
subresource calls (create / publicAccessBlock / policy / website /
ListObjectsV2) and GCS's JSON API v1 (insert / patch / iam / media upload /
list / delete), including the OAuth token endpoint for GCS. No network, no
real credentials, no docker.
"""
from __future__ import annotations

import asyncio
import json
import sys
import urllib.parse
from pathlib import Path

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.targets import aws_s3  # noqa: E402
from app.targets.aws_s3 import S3StaticTarget  # noqa: E402
from app.targets.base import TargetDeployCtx, TargetError  # noqa: E402
from app.targets.gcp_gcs import GcsStaticTarget  # noqa: E402

BUCKET = "app.example.com"
REGION = "us-east-1"
PROJECT = "proj-1"

HTML = b"<!doctype html><h1>hi</h1>"
JS = b"console.log(1)"


def run(coro):
    return asyncio.run(coro)


def make_ctx(tmp_path: Path, hostname: str | None = BUCKET):
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True, exist_ok=True)
    (dist / "index.html").write_bytes(HTML)
    (dist / "assets" / "app.js").write_bytes(JS)
    lines: list[str] = []

    async def log(line: str) -> None:
        lines.append(line)

    ctx = TargetDeployCtx(
        project_name="listless", env_name="dev", service_name="web",
        kind="static", rd=tmp_path, hostname=hostname, static_dir=dist, log=log,
    )
    return ctx, lines


# ═════ AWS S3 ═════════════════════════════════════════════════════════════════


def s3_error(status: int, code: str, message: str = "") -> httpx.Response:
    return httpx.Response(status, content=(
        f"<Error><Code>{code}</Code>"
        f"<Message>{message or code}</Message></Error>"
    ).encode())


class FakeS3:
    """Routes MockTransport requests like S3's path-style REST API."""

    def __init__(self, *, exists: bool = False, objects: dict | None = None,
                 policy_denials: int = 0, owned_elsewhere: bool = False,
                 page_size: int = 1000):
        self.exists = exists
        self.objects = dict(objects or {})       # key -> (bytes, content-type)
        self.policy_denials = policy_denials     # AccessDenied N times first
        self.owned_elsewhere = owned_elsewhere   # create → BucketAlreadyExists
        self.page_size = page_size
        self.requests: list[tuple[str, str, str]] = []  # (method, path, query keys)
        self.pab_body = b""
        self.policy_body = b""
        self.website_body = b""
        self.deleted: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        assert request.url.host == f"s3.{REGION}.amazonaws.com"  # path-style
        method = request.method
        path = urllib.parse.unquote(request.url.path)
        params = dict(request.url.params)
        self.requests.append((method, path, "&".join(sorted(params))))
        if path == f"/{BUCKET}":
            return self._bucket_call(method, params, request)
        assert path.startswith(f"/{BUCKET}/"), f"unexpected path {path}"
        return self._object_call(method, path[len(BUCKET) + 2:], request)

    def _bucket_call(self, method: str, params: dict,
                     request: httpx.Request) -> httpx.Response:
        if method == "PUT" and not params:  # CreateBucket
            if self.owned_elsewhere:
                return s3_error(409, "BucketAlreadyExists",
                                "The requested bucket name is not available")
            if self.exists:
                return s3_error(409, "BucketAlreadyOwnedByYou",
                                "Your previous request succeeded")
            self.exists = True
            return httpx.Response(200)
        if not self.exists:
            return s3_error(404, "NoSuchBucket", "does not exist")
        if method == "PUT" and "publicAccessBlock" in params:
            self.pab_body = request.content
            return httpx.Response(200)
        if method == "PUT" and "policy" in params:
            if self.policy_denials > 0:
                self.policy_denials -= 1
                return s3_error(403, "AccessDenied", "Access Denied")
            self.policy_body = request.content
            return httpx.Response(200)
        if method == "PUT" and "website" in params:
            self.website_body = request.content
            return httpx.Response(200)
        if method == "GET" and "list-type" in params:
            return self._listing(params)
        if method == "GET" and "location" in params:
            return httpx.Response(200, content=b"<LocationConstraint/>")
        if method == "DELETE" and "website" in params:
            return httpx.Response(204)
        if method == "DELETE" and not params:
            if self.objects:
                return s3_error(409, "BucketNotEmpty", "not empty")
            self.exists = False
            return httpx.Response(204)
        raise AssertionError(f"unexpected bucket call: {method} {params}")

    def _listing(self, params: dict) -> httpx.Response:
        keys = sorted(self.objects)
        start = int(params.get("continuation-token") or 0)
        page = keys[start:start + self.page_size]
        nxt = start + self.page_size
        truncated = nxt < len(keys)
        xml = ["<ListBucketResult>",
               f"<IsTruncated>{'true' if truncated else 'false'}</IsTruncated>"]
        if truncated:
            xml.append(f"<NextContinuationToken>{nxt}</NextContinuationToken>")
        xml += [f"<Contents><Key>{k}</Key></Contents>" for k in page]
        xml.append("</ListBucketResult>")
        return httpx.Response(200, content="".join(xml).encode())

    def _object_call(self, method: str, key: str,
                     request: httpx.Request) -> httpx.Response:
        if not self.exists:
            return s3_error(404, "NoSuchBucket", "does not exist")
        if method == "PUT":
            self.objects[key] = (
                request.content, request.headers.get("content-type", ""))
            return httpx.Response(200)
        if method == "DELETE":
            self.objects.pop(key, None)
            self.deleted.append(key)
            return httpx.Response(204)
        raise AssertionError(f"unexpected object call: {method} {key}")


def s3_target(fake: FakeS3, config: dict | None = None) -> S3StaticTarget:
    return S3StaticTarget(
        creds={"key_id": "AKIDEXAMPLE", "secret": "sekret", "region": REGION},
        config=config or {}, state={},
        transport=httpx.MockTransport(fake.handler),
    )


def test_s3_fresh_deploy_full_sequence(tmp_path):
    fake = FakeS3(exists=False, objects={"stale.txt": (b"old", "text/plain")})
    ctx, lines = make_ctx(tmp_path)
    result = run(s3_target(fake).deploy(ctx))

    seq = fake.requests
    assert seq[0] == ("PUT", f"/{BUCKET}", "")                    # CreateBucket
    assert seq[1] == ("PUT", f"/{BUCKET}", "publicAccessBlock")
    assert seq[2] == ("PUT", f"/{BUCKET}", "policy")
    assert seq[3] == ("PUT", f"/{BUCKET}", "website")
    assert seq[4] == ("PUT", f"/{BUCKET}/assets/app.js", "")
    assert seq[5] == ("PUT", f"/{BUCKET}/index.html", "")
    assert seq[6] == ("GET", f"/{BUCKET}", "list-type")
    assert seq[7] == ("DELETE", f"/{BUCKET}/stale.txt", "")
    assert len(seq) == 8

    # Public-access block: all four flags off.
    for flag in (b"BlockPublicAcls", b"IgnorePublicAcls",
                 b"BlockPublicPolicy", b"RestrictPublicBuckets"):
        assert b"<%s>false</%s>" % (flag, flag) in fake.pab_body
    # Policy: anonymous GetObject on the bucket's objects.
    policy = json.loads(fake.policy_body)
    (stmt,) = policy["Statement"]
    assert stmt["Effect"] == "Allow" and stmt["Principal"] == "*"
    assert stmt["Action"] == "s3:GetObject"
    assert stmt["Resource"] == f"arn:aws:s3:::{BUCKET}/*"
    # Website config: index.html for both index and SPA-fallback error page.
    assert b"<Suffix>index.html</Suffix>" in fake.website_body
    assert b"<Key>index.html</Key>" in fake.website_body

    # Uploads carry the right content and content types.
    assert fake.objects["index.html"][0] == HTML
    assert fake.objects["index.html"][1] == "text/html"
    assert fake.objects["assets/app.js"][0] == JS
    assert "javascript" in fake.objects["assets/app.js"][1]
    # Stale remote key removed.
    assert fake.deleted == ["stale.txt"]
    assert "stale.txt" not in fake.objects

    # Result contract.
    endpoint = f"{BUCKET}.s3-website-{REGION}.amazonaws.com"
    assert result.endpoint == endpoint
    assert result.cname_target == endpoint
    assert result.proxied is True
    assert result.state == {"bucket": BUCKET, "region": REGION}
    assert any("uploaded 2 file(s)" in line for line in lines)
    assert any("deleted 1 stale" in line for line in lines)


def test_s3_second_deploy_tolerates_existing_bucket(tmp_path):
    fake = FakeS3(exists=True)  # CreateBucket → 409 BucketAlreadyOwnedByYou
    ctx, _ = make_ctx(tmp_path)
    result = run(s3_target(fake).deploy(ctx))
    # The 409 is tolerated and the config still converges every deploy.
    assert ("PUT", f"/{BUCKET}", "publicAccessBlock") in fake.requests
    assert ("PUT", f"/{BUCKET}", "policy") in fake.requests
    assert ("PUT", f"/{BUCKET}", "website") in fake.requests
    assert fake.deleted == []  # nothing stale
    assert result.state["bucket"] == BUCKET


def test_s3_bucket_name_is_lowercased_hostname(tmp_path):
    fake = FakeS3(exists=False)
    ctx, _ = make_ctx(tmp_path, hostname="App.Example.COM")
    result = run(s3_target(fake).deploy(ctx))  # fake asserts on /app.example.com
    assert result.state["bucket"] == BUCKET


def test_s3_deploy_without_hostname_raises(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no request expected")

    t = S3StaticTarget(creds={"key_id": "k", "secret": "s", "region": REGION},
                       transport=httpx.MockTransport(handler))
    ctx, _ = make_ctx(tmp_path, hostname=None)
    with pytest.raises(TargetError) as exc:
        run(t.deploy(ctx))
    assert "hostname" in str(exc.value)


def test_s3_deploy_without_static_dir_raises(tmp_path):
    t = S3StaticTarget(creds={"key_id": "k", "secret": "s", "region": REGION})
    ctx, _ = make_ctx(tmp_path)
    ctx.static_dir = None
    with pytest.raises(TargetError) as exc:
        run(t.deploy(ctx))
    assert "static_dir" in str(exc.value)


def test_s3_policy_write_retries_after_access_denied(tmp_path, monkeypatch):
    monkeypatch.setattr(aws_s3, "POLICY_RETRY_INTERVAL", 0)
    fake = FakeS3(exists=False, policy_denials=2)
    ctx, _ = make_ctx(tmp_path)
    run(s3_target(fake).deploy(ctx))
    # Denied twice, third attempt landed.
    assert [q for _, _, q in fake.requests].count("policy") == 3
    assert fake.policy_body  # the retry eventually wrote the policy


def test_s3_policy_retries_exhausted_raise(tmp_path, monkeypatch):
    monkeypatch.setattr(aws_s3, "POLICY_RETRY_INTERVAL", 0)
    fake = FakeS3(exists=False, policy_denials=99)
    ctx, _ = make_ctx(tmp_path)
    with pytest.raises(TargetError) as exc:
        run(s3_target(fake).deploy(ctx))
    assert "AccessDenied" in str(exc.value)


def test_s3_bucket_owned_by_other_account_raises_hint(tmp_path):
    fake = FakeS3(owned_elsewhere=True)
    ctx, _ = make_ctx(tmp_path)
    with pytest.raises(TargetError) as exc:
        run(s3_target(fake).deploy(ctx))
    assert "another AWS account" in str(exc.value)
    assert "hostname" in str(exc.value)


def test_s3_website_endpoint_config_override(tmp_path):
    fake = FakeS3(exists=False)
    ctx, _ = make_ctx(tmp_path)
    override = f"{BUCKET}.s3-website.eu-north-1.amazonaws.com"  # dot form
    result = run(s3_target(fake, config={"website_endpoint": override}).deploy(ctx))
    assert result.endpoint == override
    assert result.cname_target == override


def test_s3_destroy_deletes_objects_then_bucket_paginated():
    fake = FakeS3(exists=True, page_size=1, objects={
        "a.txt": (b"a", "text/plain"), "b.txt": (b"b", "text/plain")})
    run(s3_target(fake).destroy({"bucket": BUCKET, "region": REGION}))
    assert sorted(fake.deleted) == ["a.txt", "b.txt"]
    assert fake.exists is False
    # Pagination: two listing pages were fetched.
    listings = [q for m, _, q in fake.requests if m == "GET" and "list-type" in q]
    assert len(listings) == 2


def test_s3_destroy_missing_bucket_is_tolerated():
    fake = FakeS3(exists=False)
    run(s3_target(fake).destroy({"bucket": BUCKET, "region": REGION}))  # no raise
    # Stopped at the 404 listing — nothing else attempted.
    assert fake.requests == [("GET", f"/{BUCKET}", "list-type")]


def test_s3_destroy_without_state_is_a_noop():
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no request expected")

    t = S3StaticTarget(creds={"key_id": "k", "secret": "s"},
                       transport=httpx.MockTransport(handler))
    run(t.destroy({}))


def test_s3_probe_true_and_false():
    fake = FakeS3(exists=True)
    assert run(s3_target(fake).probe({"bucket": BUCKET, "region": REGION})) is True
    gone = FakeS3(exists=False)
    assert run(s3_target(gone).probe({"bucket": BUCKET, "region": REGION})) is False
    assert run(s3_target(gone).probe({})) is False


def test_s3_validate_bad_creds_mentions_integrations():
    def handler(request: httpx.Request) -> httpx.Response:
        return s3_error(403, "InvalidClientTokenId", "invalid token")

    t = S3StaticTarget(creds={"key_id": "k", "secret": "s", "region": REGION},
                       transport=httpx.MockTransport(handler))
    with pytest.raises(TargetError) as exc:
        run(t.validate())
    assert "Integrations" in str(exc.value)


# ═════ GCP GCS ════════════════════════════════════════════════════════════════


STORAGE = "/storage/v1"


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


def gcs_error(status: int, message: str) -> httpx.Response:
    return httpx.Response(status, json={
        "error": {"code": status, "message": message},
    })


class FakeGcs:
    """Routes MockTransport requests like the Cloud Storage JSON API v1 (and
    answers the OAuth token endpoint itself)."""

    def __init__(self, *, exists: bool = False, objects: dict | None = None,
                 public: bool = False, create_status: int | None = None,
                 page_size: int = 1000):
        self.exists = exists
        self.objects = dict(objects or {})   # name -> (bytes, content-type)
        self.public = public
        self.create_status = create_status   # force insert to fail (e.g. 403)
        self.page_size = page_size
        self.requests: list[tuple[str, str]] = []
        self.insert_body: dict | None = None
        self.patch_body: dict | None = None
        self.iam_put: dict | None = None
        self.deleted: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.host == "oauth2.googleapis.com":
            return httpx.Response(200, json={"access_token": "tok",
                                             "expires_in": 3600})
        assert request.url.host == "storage.googleapis.com"
        method = request.method
        path = urllib.parse.unquote(request.url.path)
        self.requests.append((method, path))

        if method == "POST" and path == f"/upload{STORAGE}/b/{BUCKET}/o":
            assert request.url.params["uploadType"] == "media"
            if not self.exists:
                return gcs_error(404, "bucket not found")
            name = request.url.params["name"]
            self.objects[name] = (
                request.content, request.headers.get("content-type", ""))
            return httpx.Response(200, json={"name": name})
        if method == "POST" and path == f"{STORAGE}/b":
            assert request.url.params["project"] == PROJECT
            if self.create_status:
                return gcs_error(self.create_status, "creation refused")
            if self.exists:
                return gcs_error(409, "You already own this bucket.")
            self.exists = True
            self.insert_body = json.loads(request.content)
            return httpx.Response(200, json={"name": BUCKET})
        if method == "PATCH" and path == f"{STORAGE}/b/{BUCKET}":
            if not self.exists:
                return gcs_error(404, "bucket not found")
            self.patch_body = json.loads(request.content)
            return httpx.Response(200, json={"name": BUCKET})
        if method == "GET" and path == f"{STORAGE}/b/{BUCKET}/iam":
            bindings = ([{"role": "roles/storage.objectViewer",
                          "members": ["allUsers"]}] if self.public else [])
            return httpx.Response(200, json={"bindings": bindings,
                                             "etag": "etag-1"})
        if method == "PUT" and path == f"{STORAGE}/b/{BUCKET}/iam":
            self.iam_put = json.loads(request.content)
            self.public = True
            return httpx.Response(200, json=self.iam_put)
        if method == "GET" and path == f"{STORAGE}/b/{BUCKET}/o":
            if not self.exists:
                return gcs_error(404, "bucket not found")
            names = sorted(self.objects)
            start = int(request.url.params.get("pageToken") or 0)
            body: dict = {"items": [{"name": n}
                                    for n in names[start:start + self.page_size]]}
            if start + self.page_size < len(names):
                body["nextPageToken"] = str(start + self.page_size)
            return httpx.Response(200, json=body)
        if method == "GET" and path == f"{STORAGE}/b/{BUCKET}":
            if self.exists:
                return httpx.Response(200, json={"name": BUCKET})
            return gcs_error(404, "bucket not found")
        if method == "DELETE" and path.startswith(f"{STORAGE}/b/{BUCKET}/o/"):
            name = path.removeprefix(f"{STORAGE}/b/{BUCKET}/o/")
            if not self.exists or name not in self.objects:
                return gcs_error(404, "object not found")
            del self.objects[name]
            self.deleted.append(name)
            return httpx.Response(204)
        if method == "DELETE" and path == f"{STORAGE}/b/{BUCKET}":
            if not self.exists:
                return gcs_error(404, "bucket not found")
            self.exists = False
            return httpx.Response(204)
        raise AssertionError(f"unexpected request: {method} {path}")


def gcs_target(fake: FakeGcs, sa: dict) -> GcsStaticTarget:
    return GcsStaticTarget(
        creds={"sa": sa}, config={}, state={},
        transport=httpx.MockTransport(fake.handler),
    )


def test_gcs_fresh_deploy_full_sequence(tmp_path, sa):
    fake = FakeGcs(exists=False, objects={"stale.txt": (b"old", "text/plain")})
    ctx, lines = make_ctx(tmp_path)
    result = run(gcs_target(fake, sa).deploy(ctx))

    # Insert carries name + website config; PATCH converges UBLA + website.
    assert fake.insert_body is not None
    assert fake.insert_body["name"] == BUCKET
    website = {"mainPageSuffix": "index.html", "notFoundPage": "index.html"}
    assert fake.insert_body["website"] == website
    assert fake.patch_body is not None
    assert fake.patch_body["website"] == website
    ubla = fake.patch_body["iamConfiguration"]["uniformBucketLevelAccess"]
    assert ubla["enabled"] is True

    # IAM: allUsers → objectViewer binding added, etag carried through.
    assert fake.iam_put is not None
    assert {"role": "roles/storage.objectViewer",
            "members": ["allUsers"]} in fake.iam_put["bindings"]
    assert fake.iam_put["etag"] == "etag-1"

    # Uploads carry the right content and content types.
    assert fake.objects["index.html"][0] == HTML
    assert fake.objects["index.html"][1] == "text/html"
    assert fake.objects["assets/app.js"][0] == JS
    assert "javascript" in fake.objects["assets/app.js"][1]
    # Stale remote object removed.
    assert fake.deleted == ["stale.txt"]
    assert "stale.txt" not in fake.objects

    # Insert → patch → iam → uploads → list → stale delete, in order.
    ops = [(m, p) for m, p in fake.requests]
    assert ops.index(("POST", f"{STORAGE}/b")) \
        < ops.index(("PATCH", f"{STORAGE}/b/{BUCKET}")) \
        < ops.index(("PUT", f"{STORAGE}/b/{BUCKET}/iam")) \
        < ops.index(("POST", f"/upload{STORAGE}/b/{BUCKET}/o")) \
        < ops.index(("GET", f"{STORAGE}/b/{BUCKET}/o")) \
        < ops.index(("DELETE", f"{STORAGE}/b/{BUCKET}/o/stale.txt"))

    # Result contract.
    assert result.endpoint == f"{BUCKET}.storage.googleapis.com"
    assert result.cname_target == "c.storage.googleapis.com"
    assert result.proxied is True
    assert result.state == {"bucket": BUCKET}
    assert any("uploaded 2 file(s)" in line for line in lines)
    assert any("deleted 1 stale" in line for line in lines)


def test_gcs_second_deploy_is_idempotent(tmp_path, sa):
    fake = FakeGcs(exists=True, public=True)  # insert → 409, binding present
    ctx, _ = make_ctx(tmp_path)
    result = run(gcs_target(fake, sa).deploy(ctx))
    # 409 tolerated, config still converged, IAM PUT skipped (already bound).
    assert fake.patch_body is not None
    assert fake.iam_put is None
    assert ("PUT", f"{STORAGE}/b/{BUCKET}/iam") not in fake.requests
    assert result.state == {"bucket": BUCKET}


def test_gcs_bucket_name_is_lowercased_hostname(tmp_path, sa):
    fake = FakeGcs(exists=False)
    ctx, _ = make_ctx(tmp_path, hostname="App.Example.COM")
    result = run(gcs_target(fake, sa).deploy(ctx))  # fake asserts app.example.com
    assert result.state["bucket"] == BUCKET


def test_gcs_deploy_without_hostname_raises(tmp_path, sa):
    ctx, _ = make_ctx(tmp_path, hostname=None)
    with pytest.raises(TargetError) as exc:
        run(gcs_target(FakeGcs(), sa).deploy(ctx))
    assert "hostname" in str(exc.value)


def test_gcs_create_403_mentions_domain_verification(tmp_path, sa):
    fake = FakeGcs(create_status=403)
    ctx, _ = make_ctx(tmp_path)
    with pytest.raises(TargetError) as exc:
        run(gcs_target(fake, sa).deploy(ctx))
    assert "verified" in str(exc.value)


def test_gcs_destroy_deletes_objects_then_bucket_paginated(sa):
    fake = FakeGcs(exists=True, page_size=1, objects={
        "a.txt": (b"a", "text/plain"), "b.txt": (b"b", "text/plain")})
    run(gcs_target(fake, sa).destroy({"bucket": BUCKET}))
    assert sorted(fake.deleted) == ["a.txt", "b.txt"]
    assert fake.exists is False
    listings = [1 for m, p in fake.requests
                if m == "GET" and p == f"{STORAGE}/b/{BUCKET}/o"]
    assert len(listings) == 2  # paginated


def test_gcs_destroy_missing_bucket_is_tolerated(sa):
    fake = FakeGcs(exists=False)
    run(gcs_target(fake, sa).destroy({"bucket": BUCKET}))  # no raise
    assert fake.requests == [("GET", f"{STORAGE}/b/{BUCKET}/o")]


def test_gcs_destroy_without_state_is_a_noop(sa):
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no request expected")

    t = GcsStaticTarget(creds={"sa": sa},
                        transport=httpx.MockTransport(handler))
    run(t.destroy({}))


def test_gcs_probe_true_and_false(sa):
    assert run(gcs_target(FakeGcs(exists=True), sa)
               .probe({"bucket": BUCKET})) is True
    assert run(gcs_target(FakeGcs(exists=False), sa)
               .probe({"bucket": BUCKET})) is False
    assert run(gcs_target(FakeGcs(exists=True), sa).probe({})) is False


def test_gcs_missing_service_account_raises():
    t = GcsStaticTarget(creds={})
    with pytest.raises(TargetError) as exc:
        run(t.validate())
    assert "service-account" in str(exc.value)
