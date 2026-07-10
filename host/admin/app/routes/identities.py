"""Identities — the whitelist of emails allowed to sign in passwordlessly via
OAuth (Google or GitHub). Managed from the admin UI; all routes require an
existing admin session. Login activity (last login, provider, count) is written
by the OAuth login flow in routes/oauth.py and surfaced here read-only."""

import re

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import require_session_api
from ..db import get_session
from ..models import Identity

router = APIRouter(prefix="/api/identities")

# Pragmatic email shape check (no DNS/deliverability — we don't ship a validator).
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _serialize(i: Identity) -> dict:
    return {
        "id": i.id,
        "email": i.email,
        "enabled": i.enabled,
        "last_login_at": i.last_login_at.isoformat() if i.last_login_at else None,
        "last_login_provider": i.last_login_provider,
        "login_count": i.login_count,
        "created_at": i.created_at.isoformat() if i.created_at else None,
    }


@router.get("")
async def list_identities(
    user: str = Depends(require_session_api),
    session: AsyncSession = Depends(get_session),
):
    rows = (await session.execute(select(Identity).order_by(Identity.created_at))).scalars().all()
    return [_serialize(i) for i in rows]


class CreateBody(BaseModel):
    email: str


@router.post("")
async def add_identity(
    body: CreateBody,
    user: str = Depends(require_session_api),
    session: AsyncSession = Depends(get_session),
):
    email = body.email.strip().lower()
    if not _EMAIL_RE.match(email):
        raise HTTPException(422, "Enter a valid email address.")
    existing = (await session.execute(select(Identity).where(Identity.email == email))).scalar_one_or_none()
    if existing:
        raise HTTPException(409, "That email is already an identity.")
    identity = Identity(email=email, enabled=True)
    session.add(identity)
    await session.commit()
    await session.refresh(identity)
    return _serialize(identity)


class EnabledBody(BaseModel):
    enabled: bool


@router.post("/{identity_id}/enabled")
async def set_enabled(
    identity_id: int,
    body: EnabledBody,
    user: str = Depends(require_session_api),
    session: AsyncSession = Depends(get_session),
):
    identity = await session.get(Identity, identity_id)
    if identity is None:
        raise HTTPException(404, "Identity not found")
    identity.enabled = body.enabled
    await session.commit()
    await session.refresh(identity)
    return _serialize(identity)


@router.delete("/{identity_id}")
async def delete_identity(
    identity_id: int,
    user: str = Depends(require_session_api),
    session: AsyncSession = Depends(get_session),
):
    identity = await session.get(Identity, identity_id)
    if identity is None:
        raise HTTPException(404, "Identity not found")
    from .. import cluster_sync
    await cluster_sync.record_tombstone(session, "identity", identity.email, commit=False)
    await session.delete(identity)
    await session.commit()
    return {"ok": True}
