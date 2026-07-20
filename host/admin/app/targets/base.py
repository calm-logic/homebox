"""Deployment-target abstraction — the contract every cloud provider implements.

A "target" is where one service of one environment runs: the homebox default
(docker compose on the cluster, handled entirely by deploy.py) or a cloud
provider resource (Cloudflare Pages project, Cloud Run service, App Runner
service, S3/GCS bucket, EC2/GCE database VM). deploy.py resolves each
service's target via targetslib.effective_targets() and, ON THE CLOUD
COORDINATOR NODE ONLY, drives the non-homebox ones through this interface.

Design rules:
  - deploy() is idempotent create-or-update against DETERMINISTIC resource
    names (homebox-<project>-<env>-<service>) — a double execution during a
    coordinator handover must converge, not duplicate.
  - destroy() is idempotent and takes the persisted state dict, not live
    context — it must work after the service/env rows are gone.
  - No provider I/O outside these methods; DB access stays in targetslib.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable


class TargetError(Exception):
    """A provider operation failed in a way the deploy should surface."""


@dataclass
class ProxyRule:
    """One cloudflared-access TCP proxy a serverless workload needs baked in:
    reach `hostname` (a tunnel TCP ingress route) via 127.0.0.1:local_port."""
    hostname: str
    local_port: int


@dataclass
class TargetDeployCtx:
    """Everything a provider needs to deploy one service. Built by deploy.py."""
    project_name: str
    env_name: str
    service_name: str
    kind: str                          # web | api | static | database | …
    rd: Path                           # repo checkout for this (project, env)
    hostname: str | None               # derived public host, None for backing services
    env_vars: dict[str, str] = field(default_factory=dict)  # AFTER cross-target rewrite
    internal_port: int | None = None
    image: str | None = None           # locally-built docker tag (container targets)
    static_dir: Path | None = None     # extracted assets (static targets)
    proxy_map: list[ProxyRule] = field(default_factory=list)
    config: dict[str, Any] = field(default_factory=dict)     # service_targets.config
    state: dict[str, Any] = field(default_factory=dict)      # prior service_targets.state
    log: Callable[[str], Awaitable[None]] | None = None      # streams into Deployment.log_tail

    @property
    def resource_name(self) -> str:
        """The deterministic provider-side name for this service's resources."""
        return f"homebox-{self.project_name}-{self.env_name}-{self.service_name}"

    async def emit(self, line: str) -> None:
        if self.log:
            await self.log(line)


@dataclass
class TargetResult:
    """What a successful deploy() reports back."""
    endpoint: str                      # provider-native host (xyz.a.run.app, <p>.pages.dev, VM IP)
    cname_target: str | None = None    # what the per-host CNAME points at (None = no DNS record)
    proxied: bool = True               # Cloudflare orange-cloud for the CNAME
    state: dict[str, Any] = field(default_factory=dict)  # merged into service_targets.state.resource_ids


class DeployTarget(ABC):
    """One (provider, variant) deployer. Constructed per deploy by
    targets.get_provider() with decrypted credentials + target config."""

    provider: str = "base"             # aws | gcp | cloudflare
    variant: str = "base"              # pages | s3 | gcs | cloud_run | app_runner | ec2_db | gce_db

    @abstractmethod
    async def validate(self) -> None:
        """Cheap credential/permission probe; raise TargetError with a
        user-actionable message on failure."""

    @abstractmethod
    async def deploy(self, ctx: TargetDeployCtx) -> TargetResult:
        """Idempotent create-or-update of the service's provider resources."""

    @abstractmethod
    async def destroy(self, state: dict[str, Any]) -> None:
        """Idempotently remove the resources recorded in `state` (a prior
        deploy's persisted resource_ids)."""

    @abstractmethod
    async def probe(self, state: dict[str, Any]) -> bool:
        """Whether the deployed resource currently looks healthy."""
