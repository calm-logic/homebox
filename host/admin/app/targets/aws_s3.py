"""AWS S3 static-site deployment target — hostname-named public website buckets.

Static services deploy as an S3 bucket named exactly after the service's
public hostname, configured for website hosting and fronted by a *proxied*
Cloudflare CNAME: Cloudflare terminates TLS, so the plain-HTTP S3 website
endpoint behind it is fine. The bucket name MUST equal the hostname — that is
how S3's website virtual-host routing resolves requests arriving via CNAME.

Deploy flow (every call goes through awslib's path-style `s3()` wrapper —
request assembly for the website/policy/public-access-block subresources
lives here, awslib only signs and sends):
  1. CreateBucket, idempotently: BucketAlreadyOwnedByYou (and other 409
     conflicts) are tolerated; BucketAlreadyExists (someone else owns the
     hostname-named bucket) surfaces a pick-a-different-hostname hint.
  2. PutPublicAccessBlock with all four flags off — new buckets block public
     policies by default — then PutBucketPolicy granting anonymous
     s3:GetObject. The policy write can race the access-block change
     (AccessDenied), so it retries briefly.
  3. PutBucketWebsite: index.html as both IndexDocument and ErrorDocument
     (SPA fallback).
  4. Upload every file under ctx.static_dir, then ListObjectsV2 and delete
     remote keys that no longer exist locally.
"""

from __future__ import annotations

import asyncio
import json
import mimetypes
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import httpx

from .awslib import AwsClient, AwsError
from .base import DeployTarget, TargetDeployCtx, TargetError, TargetResult

DEFAULT_REGION = "us-east-1"

# PutBucketPolicy can race the PutPublicAccessBlock change (S3 propagates it
# asynchronously) and come back AccessDenied — retry briefly. Module
# constants so tests can zero the interval; the attempt count keeps the loop
# bounded even then.
POLICY_RETRY_INTERVAL = 1.0
POLICY_RETRY_ATTEMPTS = 5

_S3_XMLNS = "http://s3.amazonaws.com/doc/2006-03-01/"

# All four public-access-block flags off — the bucket policy below must be
# allowed to make the bucket world-readable.
_PAB_XML = (
    f'<PublicAccessBlockConfiguration xmlns="{_S3_XMLNS}">'
    "<BlockPublicAcls>false</BlockPublicAcls>"
    "<IgnorePublicAcls>false</IgnorePublicAcls>"
    "<BlockPublicPolicy>false</BlockPublicPolicy>"
    "<RestrictPublicBuckets>false</RestrictPublicBuckets>"
    "</PublicAccessBlockConfiguration>"
)

_WEBSITE_XML = (
    f'<WebsiteConfiguration xmlns="{_S3_XMLNS}">'
    "<IndexDocument><Suffix>index.html</Suffix></IndexDocument>"
    "<ErrorDocument><Key>index.html</Key></ErrorDocument>"
    "</WebsiteConfiguration>"
)


def _public_read_policy(bucket: str) -> str:
    return json.dumps({
        "Version": "2012-10-17",
        "Statement": [{
            "Sid": "HomeboxPublicRead",
            "Effect": "Allow",
            "Principal": "*",
            "Action": "s3:GetObject",
            "Resource": f"arn:aws:s3:::{bucket}/*",
        }],
    })


def _walk_files(static_dir: Path) -> list[tuple[str, bytes, str]]:
    """Every file under static_dir as (key, content, content-type), sorted.
    Keys are slash-separated relative paths without a leading slash."""
    out: list[tuple[str, bytes, str]] = []
    for path in sorted(static_dir.rglob("*")):
        if not path.is_file():
            continue
        key = path.relative_to(static_dir).as_posix()
        ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        out.append((key, path.read_bytes(), ctype))
    return out


# S3 XML is namespaced inconsistently; match on local tag names (same
# approach as awslib's internal helpers, reimplemented here — awslib's are
# private).
def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _parse_listing(root: ET.Element) -> tuple[list[str], str | None]:
    """(keys, continuation token) from a ListObjectsV2 response; the token is
    None on the last page."""
    keys: list[str] = []
    truncated = False
    token: str | None = None
    for el in root.iter():
        name = _local(el.tag)
        if name == "Contents":
            for child in el:
                if _local(child.tag) == "Key" and child.text:
                    keys.append(child.text)
        elif name == "IsTruncated":
            truncated = (el.text or "").strip().lower() == "true"
        elif name == "NextContinuationToken":
            token = el.text
    return keys, (token if truncated else None)


class S3StaticTarget(DeployTarget):
    """AWS S3 website bucket (static services)."""

    provider = "aws"
    variant = "s3"

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
        self._region: str = (
            self._config.get("region") or creds.get("region") or DEFAULT_REGION
        )
        self._transport = transport  # injectable for tests (httpx.MockTransport)
        self._aws = AwsClient(
            creds.get("key_id") or "",
            creds.get("secret") or "",
            self._region,
            transport=transport,
        )

    # ───── plumbing ───────────────────────────────────────────────────────────

    def _client_for(self, region: str) -> AwsClient:
        """The bucket lives in the region recorded at deploy time — destroy()
        and probe() must talk to that regional endpoint, not the current
        config's."""
        if region == self._aws.region:
            return self._aws
        return AwsClient(
            self._aws.key_id, self._aws.secret, region, transport=self._transport
        )

    async def _list_keys(self, bucket: str, aws: AwsClient | None = None) -> list[str]:
        """Every object key in the bucket (ListObjectsV2, paginated)."""
        aws = aws or self._aws
        keys: list[str] = []
        token: str | None = None
        while True:
            query: dict[str, Any] = {"list-type": "2"}
            if token:
                query["continuation-token"] = token
            r = await aws.s3("GET", bucket, query=query)
            page, token = _parse_listing(ET.fromstring(r.content))
            keys.extend(page)
            if not token:
                return keys

    # ───── deploy steps ───────────────────────────────────────────────────────

    async def _ensure_bucket(self, bucket: str, ctx: TargetDeployCtx) -> None:
        body = None
        if self._region != "us-east-1":
            # us-east-1 rejects an explicit LocationConstraint; everywhere
            # else requires one.
            body = (
                f'<CreateBucketConfiguration xmlns="{_S3_XMLNS}">'
                f"<LocationConstraint>{self._region}</LocationConstraint>"
                "</CreateBucketConfiguration>"
            )
        try:
            await self._aws.s3("PUT", bucket, body=body.encode() if body else None)
            await ctx.emit(f"created bucket {bucket} in {self._region}")
        except AwsError as e:
            if e.code == "BucketAlreadyExists":
                raise TargetError(
                    f"The S3 bucket name {bucket!r} is taken by another AWS "
                    "account. Website buckets must be named after the "
                    "hostname, so give this service a different "
                    "domain/subdomain and redeploy."
                ) from e
            if e.status != 409 and e.code != "BucketAlreadyOwnedByYou":
                raise
            # BucketAlreadyOwnedByYou (or another 409 conflict from a racing
            # deploy) — the bucket is ours, converge on the config below.

    async def _open_public_access(self, bucket: str, ctx: TargetDeployCtx) -> None:
        await self._aws.s3(
            "PUT", bucket, query={"publicAccessBlock": ""},
            body=_PAB_XML.encode(),
            headers={"content-type": "application/xml"},
        )
        policy = _public_read_policy(bucket).encode()
        for attempt in range(POLICY_RETRY_ATTEMPTS):
            try:
                await self._aws.s3(
                    "PUT", bucket, query={"policy": ""}, body=policy,
                    headers={"content-type": "application/json"},
                )
                return
            except AwsError as e:
                # The access-block change propagates asynchronously; a policy
                # write that lands too early is AccessDenied. Retry briefly.
                if e.code != "AccessDenied" or attempt >= POLICY_RETRY_ATTEMPTS - 1:
                    raise
                await ctx.emit("bucket policy raced the public-access change — retrying…")
                await asyncio.sleep(POLICY_RETRY_INTERVAL)

    # ───── contract ───────────────────────────────────────────────────────────

    async def validate(self) -> None:
        if not self._aws.key_id or not self._aws.secret:
            raise TargetError(
                "AWS integration is missing its access key id or secret — "
                "reconnect the account in Integrations."
            )
        try:
            await self._aws.sts_get_caller_identity()
        except AwsError as e:
            raise TargetError(
                f"AWS credential check failed: {e} — verify the access key id "
                "and secret in Integrations."
            ) from e
        except httpx.HTTPError as e:
            raise TargetError(f"AWS credential check failed: {e}") from e

    async def deploy(self, ctx: TargetDeployCtx) -> TargetResult:
        if not ctx.hostname:
            raise TargetError(
                "S3 static hosting needs a public hostname (the bucket is "
                "named after it), but this service has none — give the "
                "service a domain/subdomain and redeploy."
            )
        if not ctx.static_dir or not Path(ctx.static_dir).is_dir():
            raise TargetError(
                "S3 static hosting needs built static assets, but this "
                "service produced no static_dir — check the service's build "
                "output."
            )
        # S3 website routing via CNAME requires bucket name == hostname
        # (bucket names are lowercase-only; hostnames are case-insensitive).
        bucket = ctx.hostname.lower()
        region = self._region
        files = _walk_files(Path(ctx.static_dir))
        if not files:
            raise TargetError(f"static_dir {ctx.static_dir} contains no files.")
        try:
            await self._ensure_bucket(bucket, ctx)
            await self._open_public_access(bucket, ctx)
            await self._aws.s3(
                "PUT", bucket, query={"website": ""},
                body=_WEBSITE_XML.encode(),
                headers={"content-type": "application/xml"},
            )

            for key, content, ctype in files:
                await self._aws.s3(
                    "PUT", bucket, key, body=content,
                    headers={"content-type": ctype},
                )
            await ctx.emit(f"uploaded {len(files)} file(s) to s3://{bucket}")

            local = {key for key, _, _ in files}
            stale = [k for k in await self._list_keys(bucket) if k not in local]
            for key in stale:
                await self._aws.s3("DELETE", bucket, key)
            if stale:
                await ctx.emit(f"deleted {len(stale)} stale object(s)")
        except AwsError as e:
            raise TargetError(f"S3 static deploy failed: {e}") from e
        except httpx.HTTPError as e:
            raise TargetError(f"S3 static deploy failed: {e}") from e

        # Classic regions serve the dash form (<bucket>.s3-website-<region>);
        # some newer regions only serve the dot form (s3-website.<region>) —
        # config["website_endpoint"] overrides when a region needs it.
        endpoint = (
            self._config.get("website_endpoint")
            or f"{bucket}.s3-website-{region}.amazonaws.com"
        )
        await ctx.emit(f"static site live behind {endpoint}")
        return TargetResult(
            endpoint=endpoint,
            cname_target=endpoint,
            proxied=True,  # Cloudflare provides TLS over the HTTP-only endpoint
            state={"bucket": bucket, "region": region},
        )

    async def destroy(self, state: dict[str, Any]) -> None:
        state = state or {}
        bucket = state.get("bucket")
        if not bucket:
            return
        aws = self._client_for(state.get("region") or self._region)
        try:
            try:
                keys = await self._list_keys(bucket, aws)
            except AwsError as e:
                if e.status == 404 or e.code == "NoSuchBucket":
                    return  # Already gone — fine.
                raise
            for key in keys:
                try:
                    await aws.s3("DELETE", bucket, key)
                except AwsError as e:
                    if e.status != 404:  # NoSuchBucket/NoSuchKey race — fine
                        raise
            try:  # best-effort — DeleteBucket below removes it anyway
                await aws.s3("DELETE", bucket, query={"website": ""})
            except AwsError:
                pass
            try:
                await aws.s3("DELETE", bucket)
            except AwsError as e:
                if e.status != 404 and e.code != "NoSuchBucket":
                    raise
        except AwsError as e:
            raise TargetError(f"S3 static destroy failed: {e}") from e
        except httpx.HTTPError as e:
            raise TargetError(f"S3 static destroy failed: {e}") from e

    async def probe(self, state: dict[str, Any]) -> bool:
        state = state or {}
        bucket = state.get("bucket")
        if not bucket:
            return False
        aws = self._client_for(state.get("region") or self._region)
        try:
            # HeadBucket-equivalent: GetBucketLocation succeeds iff the bucket
            # exists and the credentials can see it.
            await aws.s3("GET", bucket, query={"location": ""})
        except (AwsError, httpx.HTTPError):
            return False
        return True
