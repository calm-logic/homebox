"""Background resource sampler. Every SAMPLE_INTERVAL seconds it records one
MetricSample (CPU/mem/net) for each managed project's running web container, and
periodically prunes samples older than RETENTION. Started/stopped from the app
lifespan in main.py.

Docker's stats call blocks (~1s to compute a CPU delta), so each sample runs in
a worker thread via asyncio.to_thread to avoid stalling the event loop."""

import asyncio
import contextlib
import logging
from datetime import datetime, timedelta

from sqlalchemy import delete, select

from .db import SessionLocal
from .host import container_stats
from .models import Deployment, MetricSample, Repository

log = logging.getLogger("homebox.metrics")

SAMPLE_INTERVAL = 15          # seconds between samples
RETENTION = timedelta(days=7)  # how long to keep samples
PRUNE_EVERY = 40               # prune once per this many sample cycles (~10 min)


async def _running_web_containers() -> list[tuple[int, str]]:
    """(repository_id, web_container) for every managed repo whose latest
    deployment is running with a known container."""
    out: list[tuple[int, str]] = []
    async with SessionLocal() as session:
        repos = (await session.execute(
            select(Repository).where(Repository.managed == True)  # noqa: E712
        )).scalars().all()
        for repo in repos:
            dep = (await session.execute(
                select(Deployment)
                .where(Deployment.repository_id == repo.id)
                .order_by(Deployment.created_at.desc())
                .limit(1)
            )).scalar_one_or_none()
            if dep and dep.status == "running" and dep.web_container:
                out.append((repo.id, dep.web_container))
    return out


async def _sample_once() -> None:
    targets = await _running_web_containers()
    if not targets:
        return
    rows: list[MetricSample] = []
    for repo_id, container in targets:
        stats = await asyncio.to_thread(container_stats, container)
        if not stats:
            continue
        rows.append(MetricSample(
            repository_id=repo_id,
            cpu_pct=stats["cpu_pct"],
            mem_used=stats["mem_used"],
            mem_limit=stats["mem_limit"],
            net_rx=stats["net_rx"],
            net_tx=stats["net_tx"],
        ))
    if rows:
        async with SessionLocal() as session:
            session.add_all(rows)
            await session.commit()


async def _prune() -> None:
    cutoff = datetime.utcnow() - RETENTION
    async with SessionLocal() as session:
        await session.execute(delete(MetricSample).where(MetricSample.ts < cutoff))
        await session.commit()


async def sampler_loop() -> None:
    cycle = 0
    while True:
        try:
            await _sample_once()
            cycle += 1
            if cycle % PRUNE_EVERY == 0:
                await _prune()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — a bad cycle must not kill the sampler
            log.exception("metrics sampler cycle failed")
        await asyncio.sleep(SAMPLE_INTERVAL)


def start() -> asyncio.Task:
    return asyncio.create_task(sampler_loop(), name="homebox-metrics-sampler")


async def stop(task: asyncio.Task) -> None:
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
