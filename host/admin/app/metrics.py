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
from .models import Deployment, Environment, MetricSample, Project, ServiceInstance

log = logging.getLogger("homebox.metrics")

SAMPLE_INTERVAL = 15          # seconds between samples
RETENTION = timedelta(days=7)  # how long to keep samples
PRUNE_EVERY = 40               # prune once per this many sample cycles (~10 min)


async def _running_service_containers() -> list[tuple[int, int, str]]:
    """(service_id, environment_id, container_name) for every running service
    instance of a managed project's latest deployment per environment."""
    out: list[tuple[int, int, str]] = []
    async with SessionLocal() as session:
        projects = (await session.execute(
            select(Project).where(Project.managed == True)  # noqa: E712
        )).scalars().all()
        for project in projects:
            envs = (await session.execute(
                select(Environment).where(Environment.project_id == project.id)
            )).scalars().all()
            for env in envs:
                dep = (await session.execute(
                    select(Deployment)
                    .where(Deployment.environment_id == env.id)
                    .order_by(Deployment.created_at.desc())
                    .limit(1)
                )).scalar_one_or_none()
                if not dep or dep.status != "running":
                    continue
                instances = (await session.execute(
                    select(ServiceInstance).where(ServiceInstance.deployment_id == dep.id)
                )).scalars().all()
                for inst in instances:
                    if inst.service_id and inst.container_name:
                        out.append((inst.service_id, env.id, inst.container_name))
    return out


async def _sample_once() -> None:
    targets = await _running_service_containers()
    if not targets:
        return
    rows: list[MetricSample] = []
    for service_id, environment_id, container in targets:
        stats = await asyncio.to_thread(container_stats, container)
        if not stats:
            continue
        rows.append(MetricSample(
            service_id=service_id,
            environment_id=environment_id,
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
