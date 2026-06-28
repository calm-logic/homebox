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


def webhook_url() -> str | None:
    """Public URL GitHub should POST to. Requires a known admin FQDN."""
    domain = (settings.admin_domain or "").strip().strip("/")
    if not domain:
        return None
    return f"https://{domain}/api/webhooks/github"


async def sync_project_webhook(session: AsyncSession, project: Project) -> tuple[bool, str]:
    """Bring the project's GitHub push webhook in line with project.managed.
    Registers when managed (idempotent), removes when not. Best-effort — returns
    a status string; never raises so it can't break the adopt call."""
    url = webhook_url()
    if not url:
        return False, "Auto-deploy on push is disabled until the admin has a public URL (ADMIN_DOMAIN)."
    if not project.integration_id:
        return False, "Project has no source-control integration."
    integration = await session.get(Integration, project.integration_id)
    if not integration:
        return False, "Integration not found."

    token = decrypted_token(integration)
    try:
        hooks = await github.list_repo_webhooks(token, project.repo_full_name)
        ours = [h for h in hooks if (h.get("config") or {}).get("url") == url]
        if project.managed and project.auto_deploy:
            if ours:
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
