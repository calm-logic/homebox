"""Shared organization helpers used by several routers (repositories, runner,
deploy, webhooks) and the OAuth flow. Kept out of any route module to avoid
import cycles."""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .crypto import decrypt
from .github import list_org_repos
from .models import Organization, Repository


def decrypted_pat(org: Organization) -> str:
    """Decrypt an org's stored credential, stripping the `oauth:` prefix used to
    tag OAuth access tokens (they're used the same as a PAT for API calls)."""
    token = decrypt(org.pat_encrypted)
    return token[len("oauth:"):] if token.startswith("oauth:") else token


async def sync_org_repos(session: AsyncSession, org: Organization) -> int:
    """Fetch the org's repos from GitHub and upsert Repository rows. Returns the
    number of repos seen. Caller commits (this only stages adds/updates)."""
    repos = await list_org_repos(decrypted_pat(org), org.login)
    existing = {
        r.full_name: r
        for r in (await session.execute(
            select(Repository).where(Repository.organization_id == org.id)
        )).scalars()
    }
    for r in repos:
        full = r["full_name"]
        branch = r.get("default_branch") or "main"
        if full in existing:
            existing[full].default_branch = branch
        else:
            session.add(Repository(
                organization_id=org.id,
                full_name=full,
                default_branch=branch,
            ))
    return len(repos)
