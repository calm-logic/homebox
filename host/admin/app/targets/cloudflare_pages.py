"""Cloudflare Pages deployment target — static assets via the Direct Upload flow.

Implements the same protocol wrangler uses for `wrangler pages deploy`:
  1. Ensure the Pages project exists (GET → 404 → POST create).
  2. POST …/upload-token → short-lived JWT scoped to the project's asset store.
  3. Hash every file (blake2b-128 of base64(content)+extension — wrangler's
     `blake2bHex(base64Content + extension)` truncated to 32 hex chars), build
     a path→hash manifest.
  4. POST /pages/assets/check-missing (JWT auth) → hashes the store lacks.
  5. POST /pages/assets/upload (JWT auth) the missing files, base64-encoded,
     in batches.
  6. POST /pages/assets/upsert-hashes (JWT auth, best-effort) to refresh TTLs.
  7. POST …/deployments as multipart form with the manifest JSON → live deploy.
  8. Attach the custom domain (idempotently) when the service has a hostname.

Requires an API token scoped with "Cloudflare Pages: Edit" on the account —
the tunnel/DNS token homebox onboarding suggests does NOT include it, so
validate() surfaces a re-scope hint instead of a bare 403.
"""

from __future__ import annotations

import base64
import hashlib
import json
import mimetypes
import re
from pathlib import Path
from typing import Any

import httpx

from ..cloudflare import API, CloudflareError, _headers, _unwrap
from .base import DeployTarget, TargetDeployCtx, TargetError, TargetResult

# Pages project names: ≤58 chars, lowercase alphanumeric and dashes, and may
# not start or end with a dash.
_NAME_MAX = 58
# check-missing tells us which blobs to send; we send them in count-capped
# batches (the API limit is 50MB/5000 files per request — 1000 keeps each
# request comfortably small for typical static builds).
_UPLOAD_BATCH = 1000


def _project_name(ctx: TargetDeployCtx) -> str:
    """Sanitize ctx.resource_name into a valid Pages project name."""
    name = ctx.resource_name.lower()
    name = re.sub(r"[^a-z0-9-]+", "-", name)
    name = re.sub(r"-{2,}", "-", name).strip("-")
    return name[:_NAME_MAX].rstrip("-")


def _pages_hash(content: bytes, extension: str) -> str:
    """Wrangler's per-file content hash: blake2b (digest_size=16 → exactly 32
    hex chars) over the base64-encoded content concatenated with the bare
    file extension ("html", not ".html")."""
    b64 = base64.b64encode(content).decode("ascii")
    return hashlib.blake2b(
        (b64 + extension).encode("ascii"), digest_size=16
    ).hexdigest()


def _walk_assets(static_dir: Path) -> tuple[dict[str, str], dict[str, tuple[str, str]]]:
    """Returns (manifest, blobs): manifest maps "/relative/path" → hash;
    blobs maps hash → (base64 content, content type)."""
    manifest: dict[str, str] = {}
    blobs: dict[str, tuple[str, str]] = {}
    for path in sorted(static_dir.rglob("*")):
        if not path.is_file():
            continue
        rel = "/" + path.relative_to(static_dir).as_posix()
        content = path.read_bytes()
        digest = _pages_hash(content, path.suffix.lstrip("."))
        manifest[rel] = digest
        ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        blobs[digest] = (base64.b64encode(content).decode("ascii"), ctype)
    return manifest, blobs


class PagesTarget(DeployTarget):
    """Cloudflare Pages (static services)."""

    provider = "cloudflare"
    variant = "pages"

    def __init__(
        self,
        *,
        creds: dict[str, Any],
        config: dict[str, Any] | None = None,
        state: dict[str, Any] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        creds = creds or {}
        self._token: str = creds.get("token") or ""
        self._account_id: str = (
            creds.get("account_id")
            or (creds.get("config") or {}).get("account_id")
            or ""
        )
        self._config = config or {}
        self._state = state or {}
        self._transport = transport  # injectable for tests (httpx.MockTransport)

    # ───── plumbing ───────────────────────────────────────────────────────────

    def _client(self, timeout: float = 30) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=timeout, transport=self._transport)

    def _jwt_headers(self, jwt: str) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {jwt}",
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "homebox-admin",
        }

    # ───── contract ───────────────────────────────────────────────────────────

    async def validate(self) -> None:
        if not self._token or not self._account_id:
            raise TargetError(
                "Cloudflare integration is missing its API token or account id "
                "— reconnect the account in Integrations."
            )
        async with self._client(15) as c:
            r = await c.get(
                f"{API}/accounts/{self._account_id}/pages/projects",
                headers=_headers(self._token),
            )
        try:
            _unwrap(r)
        except CloudflareError as e:
            if r.status_code in (401, 403):
                raise TargetError(
                    "The Cloudflare API token cannot manage Pages. Re-scope it "
                    'with "Cloudflare Pages: Edit" on the account (My Profile → '
                    "API Tokens), then update the token in Integrations. "
                    f"({e})"
                ) from e
            raise TargetError(f"Cloudflare Pages validation failed: {e}") from e

    async def deploy(self, ctx: TargetDeployCtx) -> TargetResult:
        if not ctx.static_dir or not Path(ctx.static_dir).is_dir():
            raise TargetError(
                "Cloudflare Pages needs built static assets, but this service "
                "produced no static_dir — check the service's build output."
            )
        name = _project_name(ctx)
        account = self._account_id
        headers = _headers(self._token)
        try:
            async with self._client(60) as c:
                # 1. Ensure the project exists.
                r = await c.get(
                    f"{API}/accounts/{account}/pages/projects/{name}",
                    headers=headers,
                )
                if r.status_code == 404:
                    await ctx.emit(f"creating Pages project {name}…")
                    r = await c.post(
                        f"{API}/accounts/{account}/pages/projects",
                        headers=headers,
                        json={"name": name, "production_branch": "main"},
                    )
                _unwrap(r)

                # 2a. Short-lived JWT for the asset-store endpoints.
                r = await c.post(
                    f"{API}/accounts/{account}/pages/projects/{name}/upload-token",
                    headers=headers,
                )
                jwt = (_unwrap(r) or {}).get("jwt")
                if not jwt:
                    raise TargetError(
                        "Cloudflare did not return a Pages upload token."
                    )
                jwt_headers = self._jwt_headers(jwt)

                # 2b. Hash the build output.
                manifest, blobs = _walk_assets(Path(ctx.static_dir))
                if not manifest:
                    raise TargetError(
                        f"static_dir {ctx.static_dir} contains no files."
                    )

                # 2c. Which blobs does the store lack?
                r = await c.post(
                    f"{API}/pages/assets/check-missing",
                    headers=jwt_headers,
                    json={"hashes": list(blobs)},
                )
                missing = [h for h in (_unwrap(r) or []) if h in blobs]
                await ctx.emit(
                    f"uploading {len(manifest)} files ({len(missing)} new)…"
                )

                # 2d. Upload the missing blobs in batches.
                for i in range(0, len(missing), _UPLOAD_BATCH):
                    batch = missing[i : i + _UPLOAD_BATCH]
                    payload = [
                        {
                            "key": digest,
                            "value": blobs[digest][0],
                            "metadata": {"contentType": blobs[digest][1]},
                            "base64": True,
                        }
                        for digest in batch
                    ]
                    r = await c.post(
                        f"{API}/pages/assets/upload",
                        headers=jwt_headers,
                        json=payload,
                    )
                    _unwrap(r)

                # 2e. Refresh TTLs on everything we reference (best-effort —
                # a failure here never breaks the deploy).
                try:
                    r = await c.post(
                        f"{API}/pages/assets/upsert-hashes",
                        headers=jwt_headers,
                        json={"hashes": list(blobs)},
                    )
                    _unwrap(r)
                except (CloudflareError, httpx.HTTPError) as e:
                    await ctx.emit(f"upsert-hashes failed (non-fatal): {e}")

                # 2f. Create the deployment from the manifest (multipart form).
                dep_headers = {
                    k: v for k, v in headers.items() if k != "Content-Type"
                }
                r = await c.post(
                    f"{API}/accounts/{account}/pages/projects/{name}/deployments",
                    headers=dep_headers,
                    files={"manifest": (None, json.dumps(manifest))},
                )
                dep = _unwrap(r) or {}
                await ctx.emit(
                    f"deployment {dep.get('id') or '?'} live at {name}.pages.dev"
                )

                # 3. Custom domain (idempotent: list first, 409 tolerated).
                if ctx.hostname:
                    await self._ensure_domain(c, account, name, ctx.hostname, ctx)
        except CloudflareError as e:
            raise TargetError(f"Cloudflare Pages deploy failed: {e}") from e
        except httpx.HTTPError as e:
            raise TargetError(f"Cloudflare Pages deploy failed: {e}") from e

        return TargetResult(
            endpoint=f"{name}.pages.dev",
            cname_target=f"{name}.pages.dev",
            proxied=True,
            state={
                "pages_project": name,
                "deployment_id": dep.get("id"),
                "account_id": account,
            },
        )

    async def _ensure_domain(
        self,
        c: httpx.AsyncClient,
        account: str,
        name: str,
        hostname: str,
        ctx: TargetDeployCtx,
    ) -> None:
        headers = _headers(self._token)
        url = f"{API}/accounts/{account}/pages/projects/{name}/domains"
        r = await c.get(url, headers=headers)
        existing = {
            (d.get("name") or "").lower() for d in (_unwrap(r) or [])
        }
        if hostname.lower() in existing:
            return
        await ctx.emit(f"attaching custom domain {hostname}…")
        r = await c.post(url, headers=headers, json={"name": hostname})
        try:
            _unwrap(r)
        except CloudflareError as e:
            # Racing another deploy: the domain landed between our GET and
            # POST. Anything else propagates.
            if r.status_code == 409 or "already exists" in str(e).lower():
                return
            raise

    async def destroy(self, state: dict[str, Any]) -> None:
        state = state or {}
        name = state.get("pages_project")
        if not name:
            return
        account = state.get("account_id") or self._account_id
        async with self._client(30) as c:
            r = await c.delete(
                f"{API}/accounts/{account}/pages/projects/{name}",
                headers=_headers(self._token),
            )
        if r.status_code == 404:
            return  # Already gone — fine.
        try:
            _unwrap(r)
        except CloudflareError as e:
            raise TargetError(f"Cloudflare Pages destroy failed: {e}") from e

    async def probe(self, state: dict[str, Any]) -> bool:
        state = state or {}
        name = state.get("pages_project")
        if not name:
            return False
        account = state.get("account_id") or self._account_id
        try:
            async with self._client(15) as c:
                r = await c.get(
                    f"{API}/accounts/{account}/pages/projects/{name}",
                    headers=_headers(self._token),
                )
            if r.status_code == 404:
                return False
            project = _unwrap(r) or {}
        except (CloudflareError, httpx.HTTPError):
            return False
        return bool(
            project.get("canonical_deployment") or project.get("latest_deployment")
        )
