#!/usr/bin/env bash
# =============================================================================
# Homebox Boot Orchestrator
# =============================================================================
# Brings the whole Homebox stack up in the right order, idempotently. Intended
# to run from the `homebox.service` systemd unit at boot, but safe to run by
# hand at any time (`make boot`).
#
# Why this exists: on hosts where Docker is provided by Docker Desktop's WSL
# integration there is no native `docker.service`, so nothing guarantees the
# stack comes back after a reboot. `restart: unless-stopped` alone is not
# enough — the containers race on daemon restart (traefik needs docker-proxy
# first) and the cloudflared connector is created out-of-band by the admin app.
# This script waits for the daemon, then `compose up -d` the base infra and the
# admin stack in dependency order. The admin app reconciles cloudflared itself
# once it is up (see app/monitor.py).
#
# Steps:
#   1. Wait for the Docker daemon to accept connections (it can attach late).
#   2. Ensure the shared traefik-net network exists.
#   3. compose up base-infrastructure (docker-proxy + traefik).
#   4. compose up the admin stack (db + app).
# =============================================================================

set -uo pipefail

HOMEBOX_BASE_DIR="${HOMEBOX_BASE_DIR:-/opt/homebox}"
INFRA_DIR="${HOMEBOX_BASE_DIR}/base-infrastructure"
ADMIN_DIR="${HOMEBOX_BASE_DIR}/admin"
DOCKER_WAIT_SECONDS="${HOMEBOX_DOCKER_WAIT_SECONDS:-120}"

log() { printf '[homebox-boot] %s\n' "$*"; }

# ── 1. Wait for Docker ────────────────────────────────────────────────────────
if ! command -v docker >/dev/null 2>&1; then
    log "FATAL: docker CLI not found on PATH."
    exit 1
fi

log "Waiting up to ${DOCKER_WAIT_SECONDS}s for the Docker daemon..."
waited=0
until docker info >/dev/null 2>&1; do
    if [ "$waited" -ge "$DOCKER_WAIT_SECONDS" ]; then
        log "FATAL: Docker daemon did not become ready within ${DOCKER_WAIT_SECONDS}s."
        exit 1
    fi
    sleep 3
    waited=$((waited + 3))
done
log "Docker is ready ($(docker --version 2>/dev/null))."

# ── 2. Network ────────────────────────────────────────────────────────────────
if docker network inspect traefik-net >/dev/null 2>&1; then
    log "Network traefik-net present."
else
    docker network create traefik-net >/dev/null && log "Created network traefik-net."
fi

# ── 3. Base infrastructure (traefik + docker-proxy) ──────────────────────────
compose_up() {
    local dir="$1" label="$2"
    if [ ! -f "$dir/docker-compose.yml" ]; then
        log "WARN: no compose file at $dir — skipping $label."
        return 0
    fi
    local env_args=()
    [ -f "$dir/.env" ] && env_args=(--env-file "$dir/.env")
    log "Bringing up $label ($dir)..."
    if (cd "$dir" && docker compose "${env_args[@]}" up -d); then
        log "$label is up."
    else
        log "ERROR: failed to bring up $label."
        return 1
    fi
}

rc=0
compose_up "$INFRA_DIR" "base infrastructure" || rc=1
# ── 4. Admin stack (db + app) ────────────────────────────────────────────────
compose_up "$ADMIN_DIR" "admin stack" || rc=1

if [ "$rc" -eq 0 ]; then
    log "Homebox stack is up. Cloudflared is reconciled by the admin app once it starts."
else
    log "Homebox boot completed with errors (see above)."
fi
exit "$rc"
