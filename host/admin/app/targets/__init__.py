"""Cloud deployment targets. See base.py for the DeployTarget contract and
docs/clustering.md + the plan in the repo history for the architecture.

Registry note: providers are imported lazily inside get_provider so that a
missing/broken provider module can never take down the deploy engine for
homebox-only stacks.
"""

from __future__ import annotations

from typing import Any

from .base import DeployTarget, TargetError

# target value -> service kind -> variant. Kinds absent from a target's map
# cannot be routed there (the UI greys the option out; the API rejects it).
_VARIANTS: dict[str, dict[str, str]] = {
    "cloudflare": {"static": "pages",
                   "web": "cf_containers", "api": "cf_containers"},
    "aws": {"static": "s3", "web": "app_runner", "api": "app_runner",
            "database": "ec2_db"},
    "gcp": {"static": "gcs", "web": "cloud_run", "api": "cloud_run",
            "database": "gce_db"},
}

TARGETS = ("homebox", "cloudflare", "aws", "gcp")


def options_for_kind(kind: str) -> list[str]:
    """Targets a service of this kind may choose. homebox is always first."""
    return ["homebox"] + [t for t, kinds in _VARIANTS.items() if kind in kinds]


def variant_for(target: str, kind: str, config: dict[str, Any] | None = None) -> str | None:
    """The provider variant used for (target, kind); config.variant overrides.
    None = combination unsupported."""
    if target == "homebox":
        return None
    override = (config or {}).get("variant")
    if override:
        return override
    return _VARIANTS.get(target, {}).get(kind)


def get_provider(target: str, kind: str, *, creds: dict[str, Any],
                 config: dict[str, Any], state: dict[str, Any]) -> DeployTarget:
    """Construct the deployer for (target, kind). Lazy imports keep provider
    modules isolated from each other and from homebox-only deploys."""
    variant = variant_for(target, kind, config)
    if variant is None:
        raise TargetError(f"target {target!r} does not support {kind!r} services")
    if variant == "pages":
        from .cloudflare_pages import PagesTarget
        return PagesTarget(creds=creds, config=config, state=state)
    if variant == "cf_containers":
        from .cloudflare_containers import CfContainersTarget
        return CfContainersTarget(creds=creds, config=config, state=state)
    if variant == "s3":
        from .aws_s3 import S3StaticTarget
        return S3StaticTarget(creds=creds, config=config, state=state)
    if variant == "app_runner":
        from .aws_app_runner import AppRunnerTarget
        return AppRunnerTarget(creds=creds, config=config, state=state)
    if variant == "ec2_db":
        from .aws_ec2_db import Ec2DbTarget
        return Ec2DbTarget(creds=creds, config=config, state=state)
    if variant == "gcs":
        from .gcp_gcs import GcsStaticTarget
        return GcsStaticTarget(creds=creds, config=config, state=state)
    if variant == "cloud_run":
        from .gcp_cloud_run import CloudRunTarget
        return CloudRunTarget(creds=creds, config=config, state=state)
    if variant == "gce_db":
        from .gcp_gce_db import GceDbTarget
        return GceDbTarget(creds=creds, config=config, state=state)
    raise TargetError(f"unknown variant {variant!r}")
