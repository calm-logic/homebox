"""Theme settings — accent color persisted across sessions and shared with
the unauthenticated login page.

The login screen needs to know the chosen accent before there's a session,
so GET is intentionally public. Only the accent string is exposed; nothing
else from the settings table is reachable through this route.
"""

import re
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import require_session_api
from ..db import get_session
from ..models import Setting

router = APIRouter(prefix="/api/theme")

THEME_KEY = "theme"
HEX_PATTERN = re.compile(r"^#[0-9a-fA-F]{6}$")


async def _load(session: AsyncSession) -> dict[str, Any]:
    row = (await session.execute(select(Setting).where(Setting.key == THEME_KEY))).scalar_one_or_none()
    return dict(row.value) if row and row.value else {}


async def _save(session: AsyncSession, value: dict[str, Any]) -> None:
    row = (await session.execute(select(Setting).where(Setting.key == THEME_KEY))).scalar_one_or_none()
    if row is None:
        session.add(Setting(key=THEME_KEY, value=value))
    else:
        row.value = value
    await session.commit()


@router.get("")
async def get_theme(session: AsyncSession = Depends(get_session)):
    """Public: read accent color so the login page can apply it before auth."""
    state = await _load(session)
    return {"accent_color": state.get("accent_color")}


class ThemeBody(BaseModel):
    accent_color: str | None  # hex like "#4dd6a4"; null resets to project default


@router.post("")
async def set_theme(
    body: ThemeBody,
    user: str = Depends(require_session_api),
    session: AsyncSession = Depends(get_session),
):
    """Auth: persist the user's chosen accent. `null` clears the override
    and falls back to the CSS-defined default."""
    if body.accent_color is not None and not HEX_PATTERN.match(body.accent_color):
        raise HTTPException(400, "accent_color must be a 6-char hex string like '#4dd6a4'")
    state = await _load(session)
    if body.accent_color is None:
        state.pop("accent_color", None)
    else:
        state["accent_color"] = body.accent_color.lower()
    await _save(session, state)
    return {"accent_color": state.get("accent_color")}
