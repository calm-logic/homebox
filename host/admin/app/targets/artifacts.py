"""Build artifacts for cloud targets: static asset extraction (Pages/S3/GCS)
and container image builds (Cloud Run/App Runner). Uses the deploy engine's
_run/_nixpacks_build helpers via local imports (deploy.py imports this package
lazily, so top-level imports here would cycle).
"""

from __future__ import annotations

import json
import shlex
import shutil
from pathlib import Path

from .base import ProxyRule, TargetError

# Pinned static cloudflared binary baked into serverless wrapper images (the
# `cloudflared access tcp` client that dials homebox DBs through the tunnel).
# The Dockerfile ADDs it at BUILD time — nothing is downloaded at runtime.
# Bump both together when upgrading.
CLOUDFLARED_VERSION = "2026.7.2"
CLOUDFLARED_SHA256 = {
    # sha256 of cloudflared-linux-<arch> for CLOUDFLARED_VERSION — verify with
    # `sha256sum` against the GitHub release assets when bumping the version.
    "amd64": "ec905ea7b7e327ff8abdde8cb64697a2152de74dbcdbf6aec9db8364eb3886cd",
    "arm64": "405df476437e027fc6d18729a5a77155c0a33a6082aeee60a799a688f3052e66",
}
CLOUDFLARED_URL = (
    "https://github.com/cloudflare/cloudflared/releases/download/"
    "{version}/cloudflared-linux-{arch}"
)

WRAPPER_ENTRYPOINT_PATH = "/homebox-entrypoint.sh"
WRAPPER_ENTRYPOINT_FILE = "homebox-entrypoint.sh"
WRAPPER_DOCKERFILE = "Dockerfile.homebox-wrapper"


async def extract_static_artifacts(rd: Path, project_name: str, env_name: str,
                                   d) -> Path:
    """Produce the built static assets for a static-kind DetectedService.

    Fast path: the service is plain static files (no build_command) — use the
    source dir directly. Build path: reuse the SAME generated nginx Dockerfile
    the homebox target deploys (deploy._write_static_dockerfile), build it,
    and copy /usr/share/nginx/html out of a temporary container — identical
    build semantics on either target, no second build system."""
    from ..deploy import _run, _write_static_dockerfile

    src_dir = rd / (d.build_dir or ".")
    static_dir = src_dir / (d.static_dir or "")
    if not d.build_command and static_dir.is_dir() and any(static_dir.iterdir()):
        return static_dir

    dockerfile = _write_static_dockerfile(rd, d)
    tag = f"homebox-static-{project_name}-{env_name}-{d.name}:latest"
    code, out = await _run(
        ["docker", "build", "-t", tag, "-f", str(rd / dockerfile), str(rd)],
        timeout=1200,
    )
    if code:
        raise TargetError(f"static build failed:\n{out[-2000:]}")

    out_dir = rd / ".homebox" / f"static-{d.name}"
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.parent.mkdir(parents=True, exist_ok=True)
    cid = None
    try:
        code, out = await _run(["docker", "create", tag], timeout=60)
        if code:
            raise TargetError(f"docker create failed: {out[-500:]}")
        cid = out.strip().splitlines()[-1]
        code, out = await _run(
            ["docker", "cp", f"{cid}:/usr/share/nginx/html", str(out_dir)],
            timeout=300,
        )
        if code:
            raise TargetError(f"asset extraction failed: {out[-500:]}")
    finally:
        if cid:
            await _run(["docker", "rm", "-f", cid], timeout=60)
    # docker cp copies the html dir itself into out_dir.
    inner = out_dir / "html"
    return inner if inner.is_dir() else out_dir


async def build_cloud_image(rd: Path, project_name: str, env_name: str, d,
                            env_vars: dict[str, str]) -> str:
    """Build (locally) the image a container target will push+run, returning
    the local tag. Mirrors the homebox target's build semantics so the same
    commit produces the same image on either target:

      image ref            → returned as-is (the provider pulls it upstream —
                             no local build or push needed for public images)
      dockerfile           → docker build with the service's Dockerfile
      nixpacks (fallback)  → nixpacks build with PORT injected
      compose-origin build → docker build of the compose build context

    Apps must honour $PORT (Cloud Run/App Runner inject it), same contract as
    the homebox Traefik routing."""
    from ..deploy import _run

    if d.image and not (d.dockerfile or d.build_command):
        return d.image

    tag = f"homebox-cloud-{project_name}-{env_name}-{d.name}:latest".lower()
    ctx_dir = rd / (d.build_dir or ".")
    port = d.internal_port or 8080

    if d.dockerfile:
        code, out = await _run(
            ["docker", "build", "-t", tag,
             "-f", str(ctx_dir / d.dockerfile), str(ctx_dir)],
            timeout=1800,
        )
        if code:
            raise TargetError(f"docker build failed for {d.name}:\n{out[-2000:]}")
        return tag

    # nixpacks fallback — same builder the homebox target uses.
    code, out = await _run(
        ["nixpacks", "build", str(ctx_dir), "--name", tag,
         "--env", f"PORT={port}"],
        timeout=1800,
    )
    if code:
        raise TargetError(
            f"Nixpacks could not build '{d.name}' for its cloud target. Add a "
            f"Dockerfile or declare the build in homebox.yaml.\n{out[-2000:]}"
        )
    return tag


def render_wrapper(
    entrypoint: list | None, cmd: list | None, rules: list[ProxyRule],
) -> tuple[str, str]:
    """Pure generation of the access-proxy wrapper: (dockerfile, entrypoint.sh).

    The Dockerfile builds FROM the original image (passed as BASE_IMAGE build
    arg), ADDs the pinned static cloudflared binary (checksum-verified at build
    time — nothing downloaded at runtime), and swaps the entrypoint for a shell
    script that backgrounds one `cloudflared access tcp` proxy per ProxyRule
    before exec'ing the image's ORIGINAL entrypoint/cmd. The service token pair
    arrives via TUNNEL_SERVICE_TOKEN_ID / TUNNEL_SERVICE_TOKEN_SECRET env vars
    the provider deploy injects."""
    if not entrypoint and not cmd:
        raise TargetError(
            "base image defines neither ENTRYPOINT nor CMD — cannot wrap it "
            "with the DB access proxy"
        )
    arch = "amd64"  # Cloud Run / App Runner run linux/amd64 images
    url = CLOUDFLARED_URL.format(version=CLOUDFLARED_VERSION, arch=arch)
    sha = CLOUDFLARED_SHA256[arch]
    dockerfile = f"""\
# syntax=docker/dockerfile:1
# Generated by Homebox — wraps the app image with cloudflared Access TCP
# proxies so a serverless workload can reach homebox-hosted databases.
ARG BASE_IMAGE
FROM ${{BASE_IMAGE}}
ADD --checksum=sha256:{sha} {url} /usr/local/bin/cloudflared
COPY {WRAPPER_ENTRYPOINT_FILE} {WRAPPER_ENTRYPOINT_PATH}
RUN chmod +x /usr/local/bin/cloudflared {WRAPPER_ENTRYPOINT_PATH}
ENTRYPOINT ["{WRAPPER_ENTRYPOINT_PATH}"]
CMD []
"""
    proxy_lines = [
        "cloudflared access tcp"
        f" --hostname {r.hostname}"
        f" --url 127.0.0.1:{r.local_port}"
        ' --service-token-id "$TUNNEL_SERVICE_TOKEN_ID"'
        ' --service-token-secret "$TUNNEL_SERVICE_TOKEN_SECRET" &'
        for r in rules
    ]
    exec_line = "exec " + " ".join(
        shlex.quote(str(a)) for a in (entrypoint or []) + (cmd or [])
    )
    entrypoint_sh = "\n".join([
        "#!/bin/sh",
        "# Generated by Homebox: start the DB access proxies, then hand off",
        "# to the image's original entrypoint.",
        "set -e",
        *proxy_lines,
        exec_line,
        "",
    ])
    return dockerfile, entrypoint_sh


async def wrap_with_access_proxy(base_image: str, proxy_rules: list[ProxyRule],
                                 project_name: str, env_name: str,
                                 service_name: str, scratch_dir: Path) -> str:
    """Build the wrapper image for a serverless consumer: FROM base_image, plus
    the pinned cloudflared binary and an entrypoint that proxies each ProxyRule
    on 127.0.0.1 before exec'ing the original entrypoint/cmd (recovered via
    docker inspect so the app starts exactly as it would unwrapped). Returns
    the local tag; raises TargetError on any failure."""
    from ..deploy import _run

    code, out = await _run(
        ["docker", "inspect", "--format",
         "{{json .Config.Entrypoint}} {{json .Config.Cmd}}", base_image],
        timeout=60,
    )
    if code:
        raise TargetError(
            f"docker inspect failed for {base_image}: {out[-500:]}")
    line = out.strip().splitlines()[-1] if out.strip() else ""
    try:
        dec = json.JSONDecoder()
        entrypoint, idx = dec.raw_decode(line)
        cmd, _ = dec.raw_decode(line[idx:].lstrip())
    except (ValueError, IndexError):
        raise TargetError(
            f"could not parse entrypoint/cmd of {base_image}: {line!r}")

    dockerfile, entrypoint_sh = render_wrapper(entrypoint, cmd, proxy_rules)
    scratch_dir.mkdir(parents=True, exist_ok=True)
    (scratch_dir / WRAPPER_DOCKERFILE).write_text(dockerfile)
    (scratch_dir / WRAPPER_ENTRYPOINT_FILE).write_text(entrypoint_sh)

    tag = f"homebox-wrapped-{project_name}-{env_name}-{service_name}:latest".lower()
    code, out = await _run(
        ["docker", "build", "-t", tag,
         "--build-arg", f"BASE_IMAGE={base_image}",
         "-f", str(scratch_dir / WRAPPER_DOCKERFILE), str(scratch_dir)],
        timeout=1200,
    )
    if code:
        raise TargetError(
            f"access-proxy wrapper build failed for {service_name}:\n{out[-2000:]}")
    return tag
