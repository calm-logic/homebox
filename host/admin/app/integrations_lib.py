"""Helpers for working with Integration rows (the GitHub/GitLab/Cloudflare
connections). Kept out of the route modules to avoid import cycles."""

import re

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .crypto import decrypt
from .github import list_org_repos, list_user_orgs, list_user_repos
from .models import Integration, Project

SLUG_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")


def slugify(name: str) -> str:
    """Turn a repo name into a URL-safe project slug."""
    s = re.sub(r"[^a-z0-9-]+", "-", name.strip().lower()).strip("-")
    return s or "project"


def decrypted_token(integration: Integration) -> str:
    """Decrypt an integration's credential, stripping the `oauth:` prefix used to
    tag OAuth access tokens (they're used the same as a PAT for API calls)."""
    token = decrypt(integration.secret_encrypted or "")
    return token[len("oauth:"):] if token.startswith("oauth:") else token


async def get_integration(session: AsyncSession, provider: str, account_login: str | None = None) -> Integration | None:
    q = select(Integration).where(Integration.provider == provider)
    if account_login is not None:
        q = q.where(Integration.account_login == account_login)
    return (await session.execute(q)).scalars().first()


def is_account_scoped(integration: Integration) -> bool:
    """Account-scoped github integrations cover the connected GitHub identity:
    its own repos plus every org that granted the OAuth app access. Legacy
    rows (one per org, from the old connect flow or a PAT) stay org-scoped."""
    return (integration.config or {}).get("scope") == "account"


async def sync_github_projects(session: AsyncSession, integration: Integration) -> int:
    """Fetch the integration's visible repos from GitHub and upsert Project rows
    (managed=False until the user adopts them). Returns the number of repos seen.
    Caller commits."""
    token = decrypted_token(integration)
    if is_account_scoped(integration):
        repos = await list_user_repos(token)
        # Refresh the org list shown on the integration page (best-effort).
        try:
            orgs = [o.get("login") for o in await list_user_orgs(token) if o.get("login")]
            cfg = dict(integration.config or {})
            if cfg.get("orgs") != orgs:
                cfg["orgs"] = orgs
                integration.config = cfg
        except Exception:  # noqa: BLE001 — org list is cosmetic
            pass
    else:
        repos = await list_org_repos(token, integration.account_login or "")
    existing = {
        p.repo_full_name: p
        for p in (await session.execute(
            select(Project).where(Project.integration_id == integration.id)
        )).scalars()
    }
    # Project.name is globally unique; track used slugs to avoid collisions.
    used = {
        n for (n,) in (await session.execute(select(Project.name))).all()
    }
    for r in repos:
        full = r["full_name"]
        branch = r.get("default_branch") or "main"
        if full in existing:
            existing[full].default_branch = branch
            continue
        base = slugify(r.get("name") or full.split("/")[-1])
        name = base
        i = 2
        while name in used:
            name = f"{base}-{i}"
            i += 1
        used.add(name)
        session.add(Project(
            integration_id=integration.id,
            repo_full_name=full,
            name=name,
            default_branch=branch,
            managed=False,
        ))
    return len(repos)
