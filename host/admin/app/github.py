"""Minimal GitHub REST client. Each call takes a PAT — the caller decides
which org/repo's token to use. No retries; raises HTTPStatusError on failure."""

import base64
from typing import Any
import httpx

API = "https://api.github.com"


def _headers(token: str | None) -> dict[str, str]:
    h = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "homebox-admin",
    }
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


async def get_org(token: str, org: str) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.get(f"{API}/orgs/{org}", headers=_headers(token))
        r.raise_for_status()
        return r.json()


async def get_user(token: str) -> dict[str, Any]:
    """The token's own GitHub account (login, id, …)."""
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.get(f"{API}/user", headers=_headers(token))
        r.raise_for_status()
        return r.json()


async def list_user_orgs(token: str) -> list[dict[str, Any]]:
    """Orgs the token can see (OAuth-app-approved memberships)."""
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.get(f"{API}/user/orgs", headers=_headers(token), params={"per_page": 100})
        r.raise_for_status()
        return r.json() or []


async def list_user_repos(token: str) -> list[dict[str, Any]]:
    """Every repo the token can push code from: the account's own repos plus
    repos in any org that granted the OAuth app access. One call covers the
    whole identity — this is what account-scoped integrations sync."""
    repos: list[dict] = []
    page = 1
    async with httpx.AsyncClient(timeout=20) as c:
        while True:
            r = await c.get(
                f"{API}/user/repos",
                headers=_headers(token),
                params={"per_page": 100, "page": page,
                        "affiliation": "owner,organization_member", "sort": "full_name"},
            )
            r.raise_for_status()
            chunk = r.json()
            if not chunk:
                break
            repos.extend(chunk)
            if len(chunk) < 100:
                break
            page += 1
    return repos


async def get_repo(token: str | None, repo_full_name: str) -> dict[str, Any]:
    """Repo metadata; works unauthenticated for public repos (token=None)."""
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.get(f"{API}/repos/{repo_full_name}", headers=_headers(token))
        r.raise_for_status()
        return r.json()


async def get_readme(token: str | None, repo_full_name: str, ref: str) -> dict[str, Any]:
    """README metadata/content at a ref. GitHub returns small files base64
    encoded; callers use path to resolve relative markdown image references."""
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.get(
            f"{API}/repos/{repo_full_name}/readme", headers=_headers(token),
            params={"ref": ref},
        )
        r.raise_for_status()
        return r.json()


async def get_file_bytes(token: str | None, repo_full_name: str,
                         path: str, ref: str, max_bytes: int) -> bytes:
    """Read one repository file without exposing a private-repo token to the
    browser. Reject unexpectedly large content before decoding where possible."""
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.get(
            f"{API}/repos/{repo_full_name}/contents/{path}",
            headers=_headers(token), params={"ref": ref},
        )
        r.raise_for_status()
        data = r.json()
        if int(data.get("size") or 0) > max_bytes:
            raise ValueError("repository image is too large")
        content = str(data.get("content") or "").replace("\n", "")
        raw = base64.b64decode(content, validate=True)
        if len(raw) > max_bytes:
            raise ValueError("repository image is too large")
        return raw


async def search_public_repos(token: str | None, query: str, limit: int = 10) -> list[dict[str, Any]]:
    """Top public repos matching a query. A token just raises the rate limit
    (30/min vs 10/min); results are pinned public with is:public."""
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.get(
            f"{API}/search/repositories",
            headers=_headers(token),
            params={"q": f"{query} is:public", "per_page": limit},
        )
        r.raise_for_status()
        return (r.json() or {}).get("items", [])


async def list_org_repos(token: str, org: str) -> list[dict[str, Any]]:
    repos: list[dict] = []
    page = 1
    async with httpx.AsyncClient(timeout=20) as c:
        while True:
            r = await c.get(
                f"{API}/orgs/{org}/repos",
                headers=_headers(token),
                params={"per_page": 100, "page": page, "type": "all"},
            )
            r.raise_for_status()
            chunk = r.json()
            if not chunk:
                break
            repos.extend(chunk)
            if len(chunk) < 100:
                break
            page += 1
    return repos


async def list_org_runners(token: str, org: str) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.get(f"{API}/orgs/{org}/actions/runners", headers=_headers(token))
        r.raise_for_status()
        return r.json()


async def get_org_runner_token(token: str, org: str) -> str:
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.post(
            f"{API}/orgs/{org}/actions/runners/registration-token",
            headers=_headers(token),
        )
        r.raise_for_status()
        return r.json().get("token", "")


async def list_repo_webhooks(token: str, repo_full_name: str) -> list[dict[str, Any]]:
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.get(f"{API}/repos/{repo_full_name}/hooks", headers=_headers(token))
        r.raise_for_status()
        return r.json()


WEBHOOK_EVENTS = ["push", "workflow_run"]


async def create_repo_webhook(
    token: str, repo_full_name: str, url: str, secret: str
) -> dict[str, Any]:
    """Create the deploy webhook (push + workflow_run for check-gated deploys).
    Idempotent at the caller level (check list first)."""
    payload = {
        "name": "web",
        "active": True,
        "events": WEBHOOK_EVENTS,
        "config": {"url": url, "content_type": "json", "secret": secret, "insecure_ssl": "0"},
    }
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.post(f"{API}/repos/{repo_full_name}/hooks", headers=_headers(token), json=payload)
        r.raise_for_status()
        return r.json()


async def update_repo_webhook_events(
    token: str, repo_full_name: str, hook_id: int, events: list[str]
) -> None:
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.patch(
            f"{API}/repos/{repo_full_name}/hooks/{hook_id}",
            headers=_headers(token), json={"events": events},
        )
        r.raise_for_status()


async def count_workflows(token: str, repo_full_name: str) -> int:
    """Active workflow count — 0 means the repo has no CI to gate deploys on."""
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.get(
            f"{API}/repos/{repo_full_name}/actions/workflows",
            headers=_headers(token), params={"per_page": 100},
        )
        r.raise_for_status()
        flows = r.json().get("workflows", [])
        return sum(1 for w in flows if w.get("state") == "active")


async def dispatch_workflow(
    token: str, repo_full_name: str, workflow_file: str, ref: str,
    inputs: dict[str, str] | None = None,
) -> None:
    """Trigger a workflow_dispatch run (e.g. e2e tests against a deployed env).
    The workflow must declare `on: workflow_dispatch`."""
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.post(
            f"{API}/repos/{repo_full_name}/actions/workflows/{workflow_file}/dispatches",
            headers=_headers(token), json={"ref": ref, "inputs": inputs or {}},
        )
        r.raise_for_status()


async def list_check_runs(token: str, repo_full_name: str, sha: str) -> list[dict[str, Any]]:
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.get(
            f"{API}/repos/{repo_full_name}/commits/{sha}/check-runs",
            headers=_headers(token), params={"per_page": 100},
        )
        r.raise_for_status()
        return r.json().get("check_runs", [])


async def delete_repo_webhook(token: str, repo_full_name: str, hook_id: int) -> None:
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.delete(f"{API}/repos/{repo_full_name}/hooks/{hook_id}", headers=_headers(token))
        if r.status_code not in (204, 404):
            r.raise_for_status()


async def list_workflow_runs(
    token: str, repo_full_name: str, per_page: int = 20
) -> list[dict[str, Any]]:
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.get(
            f"{API}/repos/{repo_full_name}/actions/runs",
            headers=_headers(token),
            params={"per_page": per_page},
        )
        r.raise_for_status()
        return r.json().get("workflow_runs", [])
