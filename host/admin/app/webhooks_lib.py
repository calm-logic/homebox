"""Push-webhook plumbing: a per-install signing secret and helpers to register/
remove the GitHub webhook for a managed repo. The secret lives in the `settings`
table under key "webhook" (same JSON-blob pattern as the Cloudflare state)."""

import secrets as _secrets

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from . import github
from .config import settings
from .crypto import decrypt, encrypt
from .models import Integration, Project, Setting
from .integrations_lib import decrypted_token

WEBHOOK_SETTING_KEY = "webhook"
ADMIN_DOMAIN_KEY = "admin_domain"  # Setting written by onboarding (routes/onboarding.py)


async def get_or_create_webhook_secret(session: AsyncSession) -> str:
    row = (await session.execute(select(Setting).where(Setting.key == WEBHOOK_SETTING_KEY))).scalar_one_or_none()
    if row and row.value and row.value.get("secret_encrypted"):
        return decrypt(row.value["secret_encrypted"])
    secret = _secrets.token_hex(32)
    value = {"secret_encrypted": encrypt(secret)}
    if row is None:
        session.add(Setting(key=WEBHOOK_SETTING_KEY, value=value))
    else:
        row.value = value
    await session.commit()
    return secret


async def webhook_url(session: AsyncSession) -> str | None:
    """Public URL GitHub should POST to. Reads the admin FQDN the onboarding
    wizard stored in the settings table, falling back to the ADMIN_DOMAIN env."""
    row = (
        await session.execute(select(Setting).where(Setting.key == ADMIN_DOMAIN_KEY))
    ).scalar_one_or_none()
    domain = row.value if row and isinstance(row.value, str) else ""
    domain = (domain or settings.admin_domain or "").strip().strip("/")
    if not domain:
        return None
    return f"https://{domain}/api/webhooks/github"


async def sync_project_webhook(session: AsyncSession, project: Project) -> tuple[bool, str]:
    """Bring the project's GitHub push webhook in line with project.managed.
    Registers when managed (idempotent), removes when not. Best-effort — returns
    a status string; never raises so it can't break the adopt call."""
    url = await webhook_url(session)
    if not url:
        return False, "Auto-deploy on push is disabled until the admin has a public URL (set one in onboarding)."
    if not project.integration_id:
        return False, "Public repo (no integration): push webhooks aren't available — deploy manually."
    integration = await session.get(Integration, project.integration_id)
    if not integration:
        return False, "Integration not found."

    token = decrypted_token(integration)
    try:
        hooks = await github.list_repo_webhooks(token, project.repo_full_name)
        ours = [h for h in hooks if (h.get("config") or {}).get("url") == url]
        if project.managed and project.auto_deploy:
            if ours:
                # Upgrade hooks created before check-gated deploys existed.
                missing = set(github.WEBHOOK_EVENTS) - set(ours[0].get("events") or [])
                if missing:
                    await github.update_repo_webhook_events(
                        token, project.repo_full_name, ours[0]["id"], github.WEBHOOK_EVENTS
                    )
                    return True, "Webhook updated."
                return True, "Webhook already registered."
            secret = await get_or_create_webhook_secret(session)
            await github.create_repo_webhook(token, project.repo_full_name, url, secret)
            return True, "Webhook registered."
        else:
            for h in ours:
                await github.delete_repo_webhook(token, project.repo_full_name, h["id"])
            return True, "Webhook removed."
    except httpx.HTTPStatusError as e:
        # 403 most likely = token lacks admin:repo_hook.
        return False, f"GitHub webhook API error: {e.response.status_code} (token needs admin:repo_hook scope)."
    except httpx.HTTPError as e:
        return False, f"GitHub webhook request failed: {e}"
