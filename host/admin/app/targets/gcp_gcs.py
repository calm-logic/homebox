"""GCP Cloud Storage static-site deployment target — hostname-named buckets.

Static services deploy as a GCS bucket named exactly after the service's
public hostname, served through GCS's CNAME redirect endpoint and fronted by
a *proxied* Cloudflare CNAME to c.storage.googleapis.com: Cloudflare
terminates TLS, so GCS's plain-HTTP CNAME serving is fine. The bucket name
MUST equal the hostname — that is how c.storage.googleapis.com resolves
which bucket serves the request.

Deploy flow (Cloud Storage JSON API v1 via gcplib.GcpClient):
  1. Insert the bucket (409 tolerated) with website config
     {mainPageSuffix: index.html, notFoundPage: index.html} — the 404 page is
     index.html on purpose (SPA fallback). A 403 on create usually means the
     domain is not verified for the service account, which Google requires
     for domain-named buckets — surfaced as a hint.
  2. PATCH the bucket every deploy to converge website config + uniform
     bucket-level access (adopted/pre-existing buckets included).
  3. Grant allUsers roles/storage.objectViewer (idempotent: skipped when the
     binding already exists).
  4. Upload every file under ctx.static_dir (media upload), then list and
     delete remote objects that no longer exist locally.
"""

from __future__ import annotations

import mimetypes
import urllib.parse
from pathlib import Path
from typing import Any

import httpx

from .base import DeployTarget, TargetDeployCtx, TargetError, TargetResult
from .gcplib import GcpClient, GcpError

# Serving GCS buckets over a CNAME always targets this shared endpoint; the
# Host header (== bucket name) picks the bucket.
CNAME_TARGET = "c.storage.googleapis.com"

_VIEWER_ROLE = "roles/storage.objectViewer"
_WEBSITE = {"mainPageSuffix": "index.html", "notFoundPage": "index.html"}
_BUCKET_CONFIG = {
    "website": _WEBSITE,
    "iamConfiguration": {"uniformBucketLevelAccess": {"enabled": True}},
}


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


class GcsStaticTarget(DeployTarget):
    """Google Cloud Storage website bucket (static services)."""

    provider = "gcp"
    variant = "gcs"

    def __init__(
        self,
        *,
        creds: dict[str, Any],
        config: dict[str, Any] | None = None,
        state: dict[str, Any] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        creds = creds or {}
        sa = creds.get("sa") or {}
        self._config = config or {}
        self._state = state or {}
        # injectable for tests (httpx.MockTransport)
        self._gcp: GcpClient | None = GcpClient(sa, transport=transport) if sa else None

    # ───── plumbing ───────────────────────────────────────────────────────────

    def _client(self) -> GcpClient:
        if self._gcp is None:
            raise TargetError(
                "GCP integration is missing its service-account key — "
                "reconnect the account in Integrations."
            )
        return self._gcp

    async def _list_objects(self, gcp: GcpClient, bucket: str) -> list[str]:
        """Every object name in the bucket (paginated)."""
        names: list[str] = []
        token: str | None = None
        while True:
            params: dict[str, Any] = {"fields": "items/name,nextPageToken"}
            if token:
                params["pageToken"] = token
            r = await gcp.storage("GET", f"b/{bucket}/o", params=params)
            body = r.json() or {}
            names.extend(
                o["name"] for o in body.get("items") or [] if o.get("name")
            )
            token = body.get("nextPageToken")
            if not token:
                return names

    async def _delete_object(self, gcp: GcpClient, bucket: str, name: str) -> None:
        """Delete one object, tolerating 404 (raced or already gone)."""
        try:
            await gcp.storage(
                "DELETE", f"b/{bucket}/o/{urllib.parse.quote(name, safe='')}"
            )
        except GcpError as e:
            if e.status != 404:
                raise

    # ───── deploy steps ───────────────────────────────────────────────────────

    async def _ensure_bucket(self, gcp: GcpClient, bucket: str,
                             ctx: TargetDeployCtx) -> None:
        body = {"name": bucket, **_BUCKET_CONFIG}
        try:
            await gcp.storage(
                "POST", "b", params={"project": gcp.project_id}, json=body
            )
            await ctx.emit(f"created bucket {bucket}")
        except GcpError as e:
            if e.status == 409:
                pass  # already exists — the PATCH below converges its config
            elif e.status == 403:
                raise TargetError(
                    f"GCP refused to create bucket {bucket!r}: hostname-named "
                    "buckets require the domain to be verified for the "
                    "service account. Verify the domain in Google Search "
                    "Console and add the service account's email as an owner, "
                    f"then redeploy. ({e})"
                ) from e
            else:
                raise
        # Converge config on every deploy (covers adopted buckets and config
        # drift), not just on create.
        await gcp.storage("PATCH", f"b/{bucket}", json=dict(_BUCKET_CONFIG))

    async def _make_public(self, gcp: GcpClient, bucket: str,
                           ctx: TargetDeployCtx) -> None:
        """Grant allUsers read on the bucket (idempotent: read-modify-write,
        skipped entirely when the binding is already there)."""
        r = await gcp.storage("GET", f"b/{bucket}/iam")
        policy = r.json() or {}
        bindings = policy.get("bindings") or []
        for binding in bindings:
            if (binding.get("role") == _VIEWER_ROLE
                    and "allUsers" in (binding.get("members") or [])):
                return
        bindings.append({"role": _VIEWER_ROLE, "members": ["allUsers"]})
        await ctx.emit(f"granting public read on {bucket}…")
        payload: dict[str, Any] = {"bindings": bindings}
        if policy.get("etag"):
            payload["etag"] = policy["etag"]
        await gcp.storage("PUT", f"b/{bucket}/iam", json=payload)

    # ───── contract ───────────────────────────────────────────────────────────

    async def validate(self) -> None:
        gcp = self._client()
        try:
            await gcp.get_project()
        except GcpError as e:
            if e.status in (401, 403):
                raise TargetError(
                    f"The GCP service account cannot access project "
                    f"{gcp.project_id}. Grant it the Storage Admin role, then "
                    f"update the key in Integrations. ({e})"
                ) from e
            raise TargetError(f"GCP validation failed: {e}") from e
        except httpx.HTTPError as e:
            raise TargetError(f"GCP validation failed: {e}") from e

    async def deploy(self, ctx: TargetDeployCtx) -> TargetResult:
        if not ctx.hostname:
            raise TargetError(
                "GCS static hosting needs a public hostname (the bucket is "
                "named after it), but this service has none — give the "
                "service a domain/subdomain and redeploy."
            )
        if not ctx.static_dir or not Path(ctx.static_dir).is_dir():
            raise TargetError(
                "GCS static hosting needs built static assets, but this "
                "service produced no static_dir — check the service's build "
                "output."
            )
        gcp = self._client()
        # CNAME serving requires bucket name == hostname (bucket names are
        # lowercase-only; hostnames are case-insensitive).
        bucket = ctx.hostname.lower()
        files = _walk_files(Path(ctx.static_dir))
        if not files:
            raise TargetError(f"static_dir {ctx.static_dir} contains no files.")
        try:
            await self._ensure_bucket(gcp, bucket, ctx)
            await self._make_public(gcp, bucket, ctx)

            for key, content, ctype in files:
                await gcp.storage_upload(bucket, key, content, ctype)
            await ctx.emit(f"uploaded {len(files)} file(s) to gs://{bucket}")

            local = {key for key, _, _ in files}
            stale = [
                name for name in await self._list_objects(gcp, bucket)
                if name not in local
            ]
            for name in stale:
                await self._delete_object(gcp, bucket, name)
            if stale:
                await ctx.emit(f"deleted {len(stale)} stale object(s)")
        except GcpError as e:
            raise TargetError(f"GCS static deploy failed: {e}") from e
        except httpx.HTTPError as e:
            raise TargetError(f"GCS static deploy failed: {e}") from e

        endpoint = f"{bucket}.storage.googleapis.com"
        await ctx.emit(f"static site live behind {endpoint}")
        return TargetResult(
            endpoint=endpoint,
            cname_target=CNAME_TARGET,
            proxied=True,  # Cloudflare provides TLS over GCS's HTTP CNAME serving
            state={"bucket": bucket},
        )

    async def destroy(self, state: dict[str, Any]) -> None:
        state = state or {}
        bucket = state.get("bucket")
        if not bucket:
            return
        gcp = self._client()
        try:
            try:
                names = await self._list_objects(gcp, bucket)
            except GcpError as e:
                if e.status == 404:
                    return  # Already gone — fine.
                raise
            for name in names:
                await self._delete_object(gcp, bucket, name)
            try:
                await gcp.storage("DELETE", f"b/{bucket}")
            except GcpError as e:
                if e.status != 404:
                    raise
        except GcpError as e:
            raise TargetError(f"GCS static destroy failed: {e}") from e
        except httpx.HTTPError as e:
            raise TargetError(f"GCS static destroy failed: {e}") from e

    async def probe(self, state: dict[str, Any]) -> bool:
        state = state or {}
        bucket = state.get("bucket")
        if not bucket or self._gcp is None:
            return False
        try:
            await self._gcp.storage("GET", f"b/{bucket}")
        except (GcpError, httpx.HTTPError):
            return False
        return True
