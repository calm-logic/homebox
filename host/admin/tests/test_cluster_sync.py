"""Regression tests for cluster-sync newer-wins conflict resolution.

The bug: projects/environments/domains had no updated_at, so any
overwrite-mode import (initial join, deploy fan-out pull) replaced local rows
with the exporter's copy wholesale — a stale peer snapshot reverted user edits
(the reported symptom: a project's dedicated-domain setting reset to the
default domain after a deploy). These tests pin the fix: a stale export never
clobbers a fresher local edit in ANY mode, and newer rows now propagate on the
periodic update-mode reconcile too.

Runs on in-memory sqlite (aiosqlite) — no Postgres or cluster needed.
"""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from app.db import Base  # noqa: E402
from app import cluster_sync  # noqa: E402
from app.models import Domain, Environment, Project  # noqa: E402

T0 = datetime(2026, 7, 1, 12, 0, 0)          # stale export timestamp
T1 = T0 + timedelta(hours=1)                  # local user edit (newer)
T2 = T0 + timedelta(hours=2)                  # even newer export


_ENGINES: list = []


def run(coro):
    async def main():
        try:
            return await coro
        finally:
            while _ENGINES:
                await _ENGINES.pop().dispose()
    return asyncio.run(main())


async def make_session():
    engine = create_async_engine("sqlite+aiosqlite://")
    _ENGINES.append(engine)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return engine, async_sessionmaker(engine, expire_on_commit=False)()


def project_item(**over):
    item = {
        "repo_full_name": "al/listless", "name": "listless",
        "default_branch": "main", "integration": None,
        "domain_name": None, "domain_mode": "container",
        "managed": True, "auto_deploy": True, "require_checks": True,
        "description": None, "detected_stack": {}, "dissected_at": None,
        "updated_at": None,
    }
    item.update(over)
    return item


def domain_item(name, **over):
    item = {
        "name": name, "is_primary": False, "cloudflare_routed": False,
        "zone_status": "active", "zone_id": None, "name_servers": None,
        "updated_at": None,
    }
    item.update(over)
    return item


def env_item(**over):
    item = {
        "project_name": "listless", "name": "production", "kind": "production",
        "branch": None, "domain_name": None, "promotion_gate": False,
        "e2e_workflow": None, "promote_from": None, "slug_suffix": "",
        "is_default": False, "updated_at": None,
    }
    item.update(over)
    return item


async def seed_listless(session, *, domain_mode="base", domain="listless.app",
                        updated_at=T1):
    """Local state: user pointed listless at a dedicated domain at T1."""
    d_shared = Domain(name="calmlogic.dev", is_primary=True)
    d_dedicated = Domain(name=domain, updated_at=updated_at)
    session.add_all([d_shared, d_dedicated])
    await session.flush()
    p = Project(repo_full_name="al/listless", name="listless", managed=True,
                domain_id=d_dedicated.id, domain_mode=domain_mode,
                updated_at=updated_at)
    session.add(p)
    await session.commit()
    return p, d_dedicated


# ── the reported bug ─────────────────────────────────────────────────────────

def test_stale_deploy_import_does_not_reset_dedicated_domain():
    """A deploy fan-out pull carrying a STALE project row (no dedicated domain,
    shared URL mode) must not revert the local newer edit."""
    async def body():
        _, session = await make_session()
        p, d = await seed_listless(session)
        data = {
            "domains": [domain_item("calmlogic.dev", is_primary=True)],
            "projects": [project_item(updated_at=T0.isoformat())],  # stale copy
        }
        await cluster_sync.import_state(session, data, mode="deploy")
        await session.refresh(p)
        assert p.domain_mode == "base"
        assert p.domain_id == d.id          # dedicated domain survived
        assert p.updated_at == T1           # local edit timestamp untouched
    run(body())


def test_stale_full_import_does_not_reset_either():
    async def body():
        _, session = await make_session()
        p, d = await seed_listless(session)
        data = {"projects": [project_item(updated_at=T0.isoformat())]}
        await cluster_sync.import_state(session, data, mode="full")
        await session.refresh(p)
        assert p.domain_mode == "base" and p.domain_id == d.id
    run(body())


def test_untimestamped_export_loses_to_local_edit_in_overwrite_mode():
    """A pre-upgrade peer (exports no updated_at) can't clobber an edited row."""
    async def body():
        _, session = await make_session()
        p, d = await seed_listless(session)
        data = {"projects": [project_item()]}  # updated_at: None
        await cluster_sync.import_state(session, data, mode="deploy")
        await session.refresh(p)
        assert p.domain_mode == "base" and p.domain_id == d.id
    run(body())


def test_newer_export_applies_and_update_mode_propagates():
    """The flip side: genuinely newer edits win — and now travel on the
    periodic update-mode reconcile, not just deploys."""
    async def body():
        _, session = await make_session()
        p, _ = await seed_listless(session)
        data = {
            "domains": [domain_item("calmlogic.dev", is_primary=True)],
            "projects": [project_item(domain_name="calmlogic.dev",
                                      domain_mode="container",
                                      updated_at=T2.isoformat())],
        }
        await cluster_sync.import_state(session, data, mode="update")
        await session.refresh(p)
        assert p.domain_mode == "container"
        assert p.updated_at == T2
    run(body())


def test_legacy_rows_keep_old_semantics():
    """Both sides untimestamped: overwrite modes still overwrite (legacy
    behaviour), update mode stays additive-only."""
    async def body():
        _, session = await make_session()
        p, _ = await seed_listless(session, updated_at=None)
        stale = {"projects": [project_item(domain_mode="container")]}
        await cluster_sync.import_state(session, stale, mode="update")
        await session.refresh(p)
        assert p.domain_mode == "base"      # update: additive only
        await cluster_sync.import_state(session, stale, mode="deploy")
        await session.refresh(p)
        assert p.domain_mode == "container"  # overwrite: exporter wins
    run(body())


# ── cross-row postprocess gating ─────────────────────────────────────────────

def test_stale_export_cannot_flip_primary_domain():
    async def body():
        _, session = await make_session()
        d1 = Domain(name="old-primary.dev", is_primary=False, updated_at=T1)
        d2 = Domain(name="new-primary.dev", is_primary=True, updated_at=T1)
        session.add_all([d1, d2])
        await session.commit()
        stale = {"domains": [
            domain_item("old-primary.dev", is_primary=True, updated_at=T0.isoformat()),
            domain_item("new-primary.dev", is_primary=False, updated_at=T0.isoformat()),
        ]}
        await cluster_sync.import_state(session, stale, mode="deploy")
        await session.refresh(d1)
        await session.refresh(d2)
        assert d2.is_primary and not d1.is_primary
    run(body())


def test_stale_env_row_does_not_rewire_promote_from():
    async def body():
        _, session = await make_session()
        p, _ = await seed_listless(session)
        dev = Environment(project_id=p.id, name="dev", kind="dev")
        prod = Environment(project_id=p.id, name="production", kind="production",
                           promote_from_env_id=None, updated_at=T1)
        session.add_all([dev, prod])
        await session.commit()
        stale = {"environments": [
            env_item(promote_from="dev", updated_at=T0.isoformat()),
        ]}
        await cluster_sync.import_state(session, stale, mode="deploy")
        await session.refresh(prod)
        assert prod.promote_from_env_id is None
    run(body())


# ── export/import round trip ─────────────────────────────────────────────────

def test_export_ships_updated_at_and_round_trips():
    async def body():
        _, session_a = await make_session()
        p, _ = await seed_listless(session_a)
        data = await cluster_sync.export_state(session_a, node_id="node-a")
        exported = next(x for x in data["projects"]
                        if x["repo_full_name"] == "al/listless")
        assert exported["updated_at"] == T1.isoformat()
        assert any("updated_at" in d for d in data["domains"])

        # Fresh node imports it (full): row lands with the edit timestamp.
        _, session_b = await make_session()
        await cluster_sync.import_state(session_b, data, mode="full")
        from sqlalchemy import select
        got = (await session_b.execute(
            select(Project).where(Project.repo_full_name == "al/listless")
        )).scalar_one()
        assert got.domain_mode == "base"
        assert got.updated_at == T1
        # And a subsequent stale import can't undo it.
        await cluster_sync.import_state(
            session_b,
            {"projects": [project_item(updated_at=T0.isoformat())]},
            mode="deploy")
        await session_b.refresh(got)
        assert got.domain_mode == "base"
    run(body())
