"""Tests for the hand-rolled AWS SigV4 signer + client (app/targets/awslib.py).

Signature correctness is pinned against the official AWS SigV4 test suite
(the `get-vanilla` and `get-vanilla-query-order-key-case` vectors: credentials
AKIDEXAMPLE, region us-east-1, service `service`, 20150830T123600Z), so any
regression in the canonical-request / string-to-sign / key-derivation chain
fails against a known-good Authorization header. Client behavior — per-service
hosts, protocol headers, XML/JSON parsing, error mapping — runs against
httpx.MockTransport. No network, no real credentials.
"""
from __future__ import annotations

import asyncio
import hashlib
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.targets.awslib import AwsClient, AwsError, sign  # noqa: E402

# Official AWS SigV4 test-suite fixtures.
KEY_ID = "AKIDEXAMPLE"
SECRET = "wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY"
SUITE_NOW = datetime(2015, 8, 30, 12, 36, 0, tzinfo=timezone.utc)
SUITE_CRED = "AKIDEXAMPLE/20150830/us-east-1/service/aws4_request"


def run(coro):
    return asyncio.run(coro)


def suite_sign(url: str) -> dict[str, str]:
    return sign("GET", url, "us-east-1", "service", KEY_ID, SECRET,
                headers={}, body=b"", now=SUITE_NOW)


def client(handler, region="us-east-1") -> AwsClient:
    return AwsClient(KEY_ID, SECRET, region,
                     transport=httpx.MockTransport(handler))


# ───── SigV4 signer vs the official test suite ────────────────────────────────


def test_sigv4_get_vanilla():
    out = suite_sign("https://example.amazonaws.com/")
    assert out["host"] == "example.amazonaws.com"
    assert out["x-amz-date"] == "20150830T123600Z"
    assert "x-amz-content-sha256" not in out  # only added for s3
    assert out["authorization"] == (
        f"AWS4-HMAC-SHA256 Credential={SUITE_CRED}, "
        "SignedHeaders=host;x-amz-date, "
        "Signature=5fa00fa31553b73ebf1942676e86291e8372ff2a2260956d9b8aae1d763fbf31"
    )


def test_sigv4_query_params_sorted_and_encoded():
    # get-vanilla-query-order-key-case: Param2 precedes Param1 on the wire,
    # but the canonical query string sorts them.
    out = suite_sign("https://example.amazonaws.com/?Param2=value2&Param1=value1")
    assert out["authorization"] == (
        f"AWS4-HMAC-SHA256 Credential={SUITE_CRED}, "
        "SignedHeaders=host;x-amz-date, "
        "Signature=b97d918cfa904a5beff61c982a1b6f458b799221646efd99d3219ec94cdf2500"
    )
    # Wire order must not matter once canonicalized.
    swapped = suite_sign("https://example.amazonaws.com/?Param1=value1&Param2=value2")
    assert swapped == out
    # RFC3986 encoding: a raw space and its %20 form canonicalize identically.
    raw = suite_sign("https://example.amazonaws.com/?k=a b")
    enc = suite_sign("https://example.amazonaws.com/?k=a%20b")
    assert raw == enc


def test_sigv4_signs_content_type_and_x_amz_headers():
    out = sign(
        "POST", "https://ec2.us-west-2.amazonaws.com/", "us-west-2", "ec2",
        KEY_ID, SECRET,
        headers={"Content-Type": "application/x-www-form-urlencoded",
                 "X-Amz-Target": "Ignored.ByEc2",
                 "User-Agent": "homebox-admin"},  # not x-amz-*: stays unsigned
        body=b"Action=DescribeInstances&Version=2016-11-15", now=SUITE_NOW,
    )
    assert "SignedHeaders=content-type;host;x-amz-date;x-amz-target," in out["authorization"]


def test_signature_deterministic_with_pinned_now():
    args = ("PUT", "https://s3.us-east-1.amazonaws.com/bucket/key", "us-east-1", "s3",
            KEY_ID, SECRET)
    kwargs = dict(headers={}, body=b"payload", now=SUITE_NOW)
    first = sign(*args, **kwargs)
    second = sign(*args, **kwargs)
    assert first == second
    assert first["x-amz-content-sha256"] == hashlib.sha256(b"payload").hexdigest()


# ───── AwsClient over MockTransport ──────────────────────────────────────────


STS_XML = b"""\
<GetCallerIdentityResponse xmlns="https://sts.amazonaws.com/doc/2011-06-15/">
  <GetCallerIdentityResult>
    <Arn>arn:aws:iam::123456789012:user/homebox</Arn>
    <UserId>AIDAEXAMPLE</UserId>
    <Account>123456789012</Account>
  </GetCallerIdentityResult>
  <ResponseMetadata><RequestId>abc-123</RequestId></ResponseMetadata>
</GetCallerIdentityResponse>"""


def test_sts_get_caller_identity_parses_xml():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["host"] = request.url.host
        seen["auth"] = request.headers["authorization"]
        seen["body"] = request.content
        return httpx.Response(200, content=STS_XML)

    ident = run(client(handler, region="eu-west-1").sts_get_caller_identity())
    assert ident == {
        "account": "123456789012",
        "arn": "arn:aws:iam::123456789012:user/homebox",
        "user_id": "AIDAEXAMPLE",
    }
    # Global endpoint, signed as us-east-1 regardless of the client's region.
    assert seen["host"] == "sts.amazonaws.com"
    assert "/us-east-1/sts/aws4_request" in seen["auth"]
    assert b"Action=GetCallerIdentity" in seen["body"]
    assert b"Version=2011-06-15" in seen["body"]


def test_iam_global_endpoint_signs_us_east_1():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["host"] = request.url.host
        seen["auth"] = request.headers["authorization"]
        seen["body"] = request.content
        return httpx.Response(200, content=b"<CreateRoleResponse/>")

    run(client(handler, region="eu-west-1").request(
        "iam",
        body="Action=CreateRole&Version=2010-05-08",
        headers={"content-type": "application/x-www-form-urlencoded"},
    ))
    # Global endpoint, signed as us-east-1 regardless of the client's region.
    assert seen["host"] == "iam.amazonaws.com"
    assert "/us-east-1/iam/aws4_request" in seen["auth"]
    assert b"Action=CreateRole" in seen["body"]


EC2_ERROR_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<Response><Errors><Error>
  <Code>InvalidInstanceID.NotFound</Code>
  <Message>The instance ID 'i-0abc' does not exist</Message>
</Error></Errors><RequestID>req-1</RequestID></Response>"""


def test_ec2_error_raises_awserror_with_code():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, content=EC2_ERROR_XML,
                              headers={"content-type": "text/xml"})

    with pytest.raises(AwsError) as exc:
        run(client(handler, region="us-west-2").ec2(
            "DescribeInstances", {"InstanceId.1": "i-0abc"}))
    assert exc.value.status == 400
    assert exc.value.code == "InvalidInstanceID.NotFound"
    assert "does not exist" in exc.value.message


def test_ec2_success_returns_xml_root():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["host"] = request.url.host
        seen["body"] = request.content
        return httpx.Response(200, content=(
            b'<DescribeInstancesResponse xmlns="http://ec2.amazonaws.com/doc/2016-11-15/">'
            b"<reservationSet/></DescribeInstancesResponse>"))

    root = run(client(handler, region="us-west-2").ec2("DescribeInstances", {"MaxResults": "5"}))
    assert root.tag.endswith("DescribeInstancesResponse")
    assert seen["host"] == "ec2.us-west-2.amazonaws.com"
    assert b"Action=DescribeInstances" in seen["body"]
    assert b"Version=2016-11-15" in seen["body"]


def test_json_call_sets_target_and_content_type():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["host"] = request.url.host
        seen["target"] = request.headers.get("x-amz-target")
        seen["ct"] = request.headers.get("content-type")
        seen["signed"] = request.headers["authorization"]
        return httpx.Response(200, json={"ServiceSummaryList": []})

    out = run(client(handler, region="us-east-2").json_call(
        "apprunner", "AppRunner.ListServices", {"MaxResults": 5},
        json_version="1.0"))
    assert out == {"ServiceSummaryList": []}
    assert seen["host"] == "apprunner.us-east-2.amazonaws.com"
    assert seen["target"] == "AppRunner.ListServices"
    assert seen["ct"] == "application/x-amz-json-1.0"
    # Both protocol headers participate in the signature.
    assert "content-type" in seen["signed"] and "x-amz-target" in seen["signed"]


def test_json_call_default_version_is_1_1():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["ct"] = request.headers.get("content-type")
        return httpx.Response(200, json={"authorizationData": []})

    run(client(handler).json_call(
        "ecr", "AmazonEC2ContainerRegistry_V20150921.GetAuthorizationToken", {}))
    assert seen["ct"] == "application/x-amz-json-1.1"


def test_s3_put_path_style_and_payload_hash():
    body = b"hello world"
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["host"] = request.url.host
        seen["path"] = request.url.path
        seen["sha"] = request.headers.get("x-amz-content-sha256")
        seen["auth"] = request.headers["authorization"]
        seen["body"] = request.content
        return httpx.Response(200)

    r = run(client(handler).s3("PUT", "my-bucket", "releases/app.tar.gz", body=body))
    assert r.status_code == 200
    assert seen["method"] == "PUT"
    assert seen["host"] == "s3.us-east-1.amazonaws.com"  # path-style
    assert seen["path"] == "/my-bucket/releases/app.tar.gz"
    assert seen["body"] == body
    assert seen["sha"] == hashlib.sha256(body).hexdigest()
    assert "x-amz-content-sha256" in seen["auth"]  # hash header is signed


def test_non_2xx_json_error_parses_type():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={
            "__type": "com.amazonaws.apprunner#ResourceNotFoundException",
            "message": "no such service",
        })

    with pytest.raises(AwsError) as exc:
        run(client(handler).json_call(
            "apprunner", "AppRunner.DescribeService", {"ServiceArn": "arn:x"}))
    assert exc.value.status == 400
    assert exc.value.code == "ResourceNotFoundException"  # namespace stripped
    assert exc.value.message == "no such service"
    assert "ResourceNotFoundException" in str(exc.value)
