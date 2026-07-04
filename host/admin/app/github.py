"""Minimal GitHub REST client. Each call takes a PAT — the caller decides
which org/repo's token to use. No retries; raises HTTPStatusError on failure."""

from typing import Any
import httpx

API = "https://api.github.com"


def _headers(token: str) -> dict[str, str]:
    return {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "homebox-admin",
    }


async def get_org(token: str, org: str) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.get(f"{API}/orgs/{org}", headers=_headers(token))
        r.raise_for_status()
        return r.json()


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
