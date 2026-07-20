"""Minimal AWS auth + API client for cloud targets — no boto3.

Like cloudflare.py this is a hand-rolled httpx client, but AWS has no single
REST envelope: each service family speaks its own protocol over a shared
SigV4-signed HTTP layer. `AwsClient` therefore exposes one raw `request()`
plus a thin wrapper per protocol family used by the target providers:

  Query protocol (form POST, XML responses) ......... sts / iam / ec2
  JSON 1.0/1.1 (X-Amz-Target header) ................ App Runner / ECR
  REST with raw payloads (path-style URLs) .......... S3

`sign()` is a pure function implementing the SigV4 chain (canonical request
→ string-to-sign → derived signing key) exactly per the spec; the explicit
`now` parameter lets tests pin it against AWS's published signature
test-suite vectors. Payloads are always hashed — UNSIGNED-PAYLOAD is
deliberately not supported.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import posixpath
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import Any

import httpx

_ALGORITHM = "AWS4-HMAC-SHA256"


class AwsError(Exception):
    """Raised when AWS returns an error response.

    `code`/`message` are parsed from either error shape AWS uses: JSON
    (`__type` / `message`, the x-amz-json protocols) or XML
    (`<Code>` / `<Message>`, the Query protocols and S3).
    """

    def __init__(self, status: int, code: str, message: str):
        label = f"{code}: {message}" if code else message
        super().__init__(f"{label} (HTTP {status})")
        self.status = status
        self.code = code
        self.message = message


# ───── XML helpers ────────────────────────────────────────────────────────────
#
# AWS XML responses are namespaced inconsistently (STS wraps everything in a
# doc namespace, EC2 errors in none), so lookups match on local tag name.


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _find_text(root: ET.Element, name: str) -> str | None:
    for el in root.iter():
        if _local(el.tag) == name:
            return el.text
    return None


def _error_from_response(r: httpx.Response) -> AwsError:
    code, message = "", ""
    text = (r.content or b"").decode("utf-8", "replace").strip()
    if text.startswith("{"):
        try:
            data = json.loads(text)
        except ValueError:
            data = {}
        if isinstance(data, dict):
            # __type is often namespaced: "com.amazonaws.foo#NotFoundException"
            code = str(data.get("__type") or data.get("code") or "").rsplit("#", 1)[-1]
            message = str(data.get("message") or data.get("Message") or "")
    elif text.startswith("<"):
        try:
            root = ET.fromstring(text)
        except ET.ParseError:
            root = None
        if root is not None:
            code = _find_text(root, "Code") or ""
            message = _find_text(root, "Message") or ""
    if not code:
        # Some services put the type only in a header (and send empty bodies).
        code = r.headers.get("x-amzn-errortype", "").split(":")[0].rsplit("#", 1)[-1]
    return AwsError(r.status_code, code, message or f"AWS API error {r.status_code}")


# ───── SigV4 signer ───────────────────────────────────────────────────────────


def _uri_encode(value: str, *, safe: str = "") -> str:
    # RFC 3986: only unreserved chars (A-Za-z0-9-_.~) stay bare. quote()
    # already never touches those; `safe` adds "/" for path encoding.
    return urllib.parse.quote(value, safe=safe)


def _canonical_query(query: str) -> str:
    """Sorted, RFC3986-encoded query string (sort AFTER encoding, per spec)."""
    if not query:
        return ""
    pairs: list[tuple[str, str]] = []
    for part in query.split("&"):
        if not part:
            continue
        k, _, v = part.partition("=")
        pairs.append((_uri_encode(urllib.parse.unquote(k)),
                      _uri_encode(urllib.parse.unquote(v))))
    pairs.sort()
    return "&".join(f"{k}={v}" for k, v in pairs)


def _canonical_path(path: str, service: str) -> str:
    path = path or "/"
    if service == "s3":
        # S3 signs the raw path exactly as sent — no dot-segment removal, no
        # slash collapsing, no re-encoding (object keys may contain '//', '.').
        return path
    norm = posixpath.normpath(path)  # collapse '//', resolve '.' and '..'
    if path.endswith("/") and not norm.endswith("/"):
        norm += "/"
    if not norm.startswith("/"):
        norm = "/" + norm
    return _uri_encode(urllib.parse.unquote(norm), safe="/")


def _hmac(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode(), hashlib.sha256).digest()


def sign(
    method: str,
    url: str,
    region: str,
    service: str,
    key_id: str,
    secret: str,
    *,
    headers: dict[str, str],
    body: bytes,
    now: datetime | None = None,
) -> dict[str, str]:
    """Pure SigV4 signer. Returns the auth headers to send alongside `headers`:
    host, x-amz-date, authorization, and x-amz-content-sha256 for S3.

    Signed-header selection: host + x-amz-date (always), plus content-type and
    any x-amz-* headers present in `headers`. Other caller headers (accept,
    user-agent…) are sent unsigned, which AWS permits. The payload is always
    hashed into the signature.
    """
    now = now or datetime.now(timezone.utc)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    datestamp = now.strftime("%Y%m%d")

    parts = urllib.parse.urlsplit(url)
    host = parts.netloc
    payload_hash = hashlib.sha256(body).hexdigest()

    to_sign: dict[str, str] = {"host": host, "x-amz-date": amz_date}
    for name, value in headers.items():
        low = name.lower()
        if low == "content-type" or low.startswith("x-amz-"):
            to_sign[low] = value
    if service == "s3":
        to_sign["x-amz-content-sha256"] = payload_hash

    signed_names = sorted(to_sign)
    signed_headers = ";".join(signed_names)
    canonical_headers = "".join(
        f"{n}:{' '.join(to_sign[n].split())}\n" for n in signed_names
    )
    canonical_request = "\n".join([
        method.upper(),
        _canonical_path(parts.path, service),
        _canonical_query(parts.query),
        canonical_headers,
        signed_headers,
        payload_hash,
    ])

    scope = f"{datestamp}/{region}/{service}/aws4_request"
    string_to_sign = "\n".join([
        _ALGORITHM,
        amz_date,
        scope,
        hashlib.sha256(canonical_request.encode()).hexdigest(),
    ])

    key = _hmac(b"AWS4" + secret.encode(), datestamp)
    key = _hmac(key, region)
    key = _hmac(key, service)
    key = _hmac(key, "aws4_request")
    signature = hmac.new(key, string_to_sign.encode(), hashlib.sha256).hexdigest()

    out = {
        "host": host,
        "x-amz-date": amz_date,
        "authorization": (
            f"{_ALGORITHM} Credential={key_id}/{scope}, "
            f"SignedHeaders={signed_headers}, Signature={signature}"
        ),
    }
    if service == "s3":
        out["x-amz-content-sha256"] = payload_hash
    return out


# ───── Client ─────────────────────────────────────────────────────────────────


class AwsClient:
    """Signed HTTP client bound to one (key_id, secret, region).

    `transport` is injectable so tests run against httpx.MockTransport —
    no network, no credentials.
    """

    def __init__(
        self,
        key_id: str,
        secret: str,
        region: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 30.0,
    ):
        self.key_id = key_id
        self.secret = secret
        self.region = region
        self._transport = transport
        self._timeout = timeout

    def _default_host(self, service: str) -> str:
        if service in ("sts", "iam"):
            return f"{service}.amazonaws.com"  # global endpoints
        return f"{service}.{self.region}.amazonaws.com"  # s3 included: path-style

    async def request(
        self,
        service: str,
        *,
        method: str = "POST",
        path: str = "/",
        query: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        body: bytes | str | dict | None = None,
        target: str | None = None,
        host: str | None = None,
        json_version: str = "1.1",
    ) -> httpx.Response:
        """Sign + send one request. dict body → x-amz-json payload; `target`
        sets X-Amz-Target. Raises AwsError on any non-2xx response."""
        hdrs = {k.lower(): v for k, v in (headers or {}).items()}
        if isinstance(body, dict):
            body = json.dumps(body).encode()
            hdrs.setdefault("content-type", f"application/x-amz-json-{json_version}")
        elif isinstance(body, str):
            body = body.encode()
        elif body is None:
            body = b""
        if target:
            hdrs["x-amz-target"] = target

        sign_region = self.region
        if host is None:
            host = self._default_host(service)
            if service in ("sts", "iam"):
                sign_region = "us-east-1"  # the global sts/iam endpoints sign as us-east-1

        url = f"https://{host}{path}"
        if query:
            qs = "&".join(
                f"{k}={v}"
                for k, v in sorted(
                    (_uri_encode(str(k)), _uri_encode(str(v))) for k, v in query.items()
                )
            )
            url += f"?{qs}"

        hdrs.update(sign(
            method, url, sign_region, service, self.key_id, self.secret,
            headers=hdrs, body=body,
        ))
        async with httpx.AsyncClient(
            transport=self._transport, timeout=self._timeout
        ) as c:
            r = await c.request(method, url, content=body, headers=hdrs)
        if r.status_code // 100 != 2:
            raise _error_from_response(r)
        return r

    # ── convenience wrappers (one per protocol family) ────────────────────────

    async def sts_get_caller_identity(self) -> dict[str, str | None]:
        """Validate credentials and identify the account (Query protocol).
        Returns {account, arn, user_id}."""
        r = await self.request(
            "sts",
            body="Action=GetCallerIdentity&Version=2011-06-15",
            headers={"content-type": "application/x-www-form-urlencoded"},
        )
        root = ET.fromstring(r.content)
        return {
            "account": _find_text(root, "Account"),
            "arn": _find_text(root, "Arn"),
            "user_id": _find_text(root, "UserId"),
        }

    async def ec2(self, action: str, params: dict[str, str]) -> ET.Element:
        """One EC2 Query-protocol call (Version 2016-11-15). Returns the parsed
        XML root; raises AwsError when the response carries <Errors>."""
        form = {"Action": action, "Version": "2016-11-15", **params}
        r = await self.request(
            "ec2",
            body=urllib.parse.urlencode(sorted(form.items())),
            headers={"content-type": "application/x-www-form-urlencoded"},
        )
        root = ET.fromstring(r.content)
        if any(_local(el.tag) == "Errors" for el in root.iter()):
            raise AwsError(
                r.status_code,
                _find_text(root, "Code") or "",
                _find_text(root, "Message") or "EC2 returned <Errors>",
            )
        return root

    async def json_call(
        self, service: str, target: str, payload: dict, *, json_version: str = "1.1"
    ) -> dict:
        """One x-amz-json call — App Runner (`AppRunner.<Op>`, json 1.0) and
        ECR (`AmazonEC2ContainerRegistry_V20150921.<Op>`, json 1.1)."""
        r = await self.request(
            service, body=payload, target=target, json_version=json_version
        )
        if not r.content:
            return {}
        return r.json()

    async def s3(
        self,
        method: str,
        bucket: str,
        key: str = "",
        *,
        body: bytes | None = None,
        query: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        """One S3 call, path-style (`s3.{region}.amazonaws.com/{bucket}/{key}`)
        — no per-bucket DNS needed. Returns the raw response (S3 bodies are
        content, not envelopes); raises AwsError on non-2xx."""
        path = "/" + _uri_encode(bucket)
        if key:
            path += "/" + _uri_encode(key, safe="/")
        return await self.request(
            "s3", method=method, path=path, query=query, headers=headers, body=body
        )
