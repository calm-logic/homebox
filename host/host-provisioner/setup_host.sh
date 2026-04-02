#!/usr/bin/env bash
# =============================================================================
# Homebox Host Provisioner
# =============================================================================
# Run this script on the dedicated Host server to install Docker, create the
# base infrastructure directories, and print next-steps for cloudflared and
# the GitHub Actions self-hosted runner.
#
# Usage:
#   chmod +x setup_host.sh
#   sudo ./setup_host.sh
# =============================================================================

set -euo pipefail

# ---------------------------------------------------------------------------
# Colours (disabled when stdout is not a terminal)
# ---------------------------------------------------------------------------
if [ -t 1 ]; then
    GREEN='\033[0;32m'
    YELLOW='\033[1;33m'
    CYAN='\033[0;36m'
    RED='\033[0;31m'
    NC='\033[0m'
else
    GREEN='' YELLOW='' CYAN='' RED='' NC=''
fi

info()  { printf "${GREEN}[INFO]${NC}  %s\n" "$*"; }
warn()  { printf "${YELLOW}[WARN]${NC}  %s\n" "$*"; }
step()  { printf "${CYAN}[STEP]${NC}  %s\n" "$*"; }
fail()  { printf "${RED}[FAIL]${NC}  %s\n" "$*" >&2; exit 1; }

# ---------------------------------------------------------------------------
# Pre-flight checks
# ---------------------------------------------------------------------------
if [ "$(id -u)" -ne 0 ]; then
    fail "This script must be run as root (use sudo)."
fi

HOMEBOX_BASE_DIR="${HOMEBOX_BASE_DIR:-/opt/homebox}"
TRAEFIK_CONF_DIR="${HOMEBOX_BASE_DIR}/traefik"
PROJECTS_DIR="${HOMEBOX_BASE_DIR}/projects"
INFRA_DIR="${HOMEBOX_BASE_DIR}/base-infrastructure"

# ---------------------------------------------------------------------------
# 1. Install Docker & Docker Compose
# ---------------------------------------------------------------------------
step "1/5  Installing Docker"

if command -v docker &>/dev/null; then
    info "Docker is already installed: $(docker --version)"
else
    # Use the official convenience script (works on Debian, Ubuntu, Fedora, etc.)
    curl -fsSL https://get.docker.com | sh
    info "Docker installed: $(docker --version)"
fi

# Ensure the calling user (SUDO_USER) can run docker without sudo
if [ -n "${SUDO_USER:-}" ]; then
    usermod -aG docker "$SUDO_USER" 2>/dev/null || true
    info "Added $SUDO_USER to the docker group (re-login to take effect)."
fi

# Docker Compose v2 ships as a docker plugin; verify it's present
if docker compose version &>/dev/null; then
    info "Docker Compose plugin detected: $(docker compose version --short)"
else
    warn "Docker Compose plugin not found — installing via apt."
    apt-get update -qq && apt-get install -y -qq docker-compose-plugin
    info "Docker Compose installed: $(docker compose version --short)"
fi

# ---------------------------------------------------------------------------
# 2. Create directory layout
# ---------------------------------------------------------------------------
step "2/5  Creating Homebox directories under ${HOMEBOX_BASE_DIR}"

mkdir -p "$TRAEFIK_CONF_DIR" "$PROJECTS_DIR" "$INFRA_DIR"

# ---------------------------------------------------------------------------
# 3. Copy base infrastructure files
# ---------------------------------------------------------------------------
step "3/5  Deploying base infrastructure files"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC_INFRA="${SCRIPT_DIR}/base-infrastructure"

if [ ! -d "$SRC_INFRA" ]; then
    fail "Cannot find base-infrastructure/ next to this script (looked in ${SRC_INFRA})."
fi

cp -v "$SRC_INFRA/docker-compose.yml" "$INFRA_DIR/docker-compose.yml"
cp -v "$SRC_INFRA/.env.example"       "$INFRA_DIR/.env.example"

# Deploy the dynamic_conf.yml to Traefik's watched directory
cp -v "$SRC_INFRA/dynamic_conf.yml"   "$TRAEFIK_CONF_DIR/dynamic_conf.yml"

# Create .env from example if it doesn't already exist
if [ ! -f "$INFRA_DIR/.env" ]; then
    cp "$INFRA_DIR/.env.example" "$INFRA_DIR/.env"
    warn ".env created from .env.example — edit it before starting services!"
fi

info "Files deployed:"
info "  Compose file:     ${INFRA_DIR}/docker-compose.yml"
info "  Env file:         ${INFRA_DIR}/.env"
info "  Dynamic config:   ${TRAEFIK_CONF_DIR}/dynamic_conf.yml"

# ---------------------------------------------------------------------------
# 4. Create the Docker network (idempotent)
# ---------------------------------------------------------------------------
step "4/5  Ensuring traefik-net Docker network exists"

if docker network inspect traefik-net &>/dev/null; then
    info "Network traefik-net already exists."
else
    docker network create traefik-net
    info "Created network traefik-net."
fi

# ---------------------------------------------------------------------------
# 5. Print manual next-steps
# ---------------------------------------------------------------------------
step "5/5  Manual steps required"

cat <<INSTRUCTIONS

${GREEN}=============================================================================
  Homebox Host Provisioner — Complete!
=============================================================================${NC}

${YELLOW}Before starting services, complete these manual steps:${NC}

${CYAN}A) Edit your .env file${NC}
   vi ${INFRA_DIR}/.env
   - Set HOMEBOX_DOMAIN to your actual domain (e.g., example.com)
   - Generate dashboard auth:  htpasswd -nb admin yourpassword

${CYAN}B) Authenticate Cloudflare Tunnel (cloudflared)${NC}
   1. Install cloudflared:
        curl -fsSL https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64 \
          -o /usr/local/bin/cloudflared && chmod +x /usr/local/bin/cloudflared
   2. Authenticate (opens a browser):
        cloudflared tunnel login
   3. Create a tunnel:
        cloudflared tunnel create homebox
   4. Configure the tunnel to route *.YOURDOMAIN.COM to http://localhost:80
      (Traefik handles host-based routing from there):
        cat > ~/.cloudflared/config.yml <<EOF
        tunnel: <TUNNEL_ID>
        credentials-file: /root/.cloudflared/<TUNNEL_ID>.json

        ingress:
          - hostname: "*.yourdomain.com"
            service: http://localhost:80
          - service: http_status:404
        EOF
   5. Add a DNS CNAME record for *.yourdomain.com -> <TUNNEL_ID>.cfargotunnel.com
      (or use:  cloudflared tunnel route dns homebox "*.yourdomain.com")
   6. Run the tunnel as a service:
        cloudflared service install
        systemctl enable --now cloudflared

${CYAN}C) Install GitHub Actions Self-Hosted Runner${NC}
   1. Go to your GitHub org/repo -> Settings -> Actions -> Runners -> New self-hosted runner
   2. Follow the provided commands to download and configure the runner:
        mkdir -p /opt/actions-runner && cd /opt/actions-runner
        curl -o actions-runner-linux-x64.tar.gz -L <URL_FROM_GITHUB>
        tar xzf actions-runner-linux-x64.tar.gz
        ./config.sh --url https://github.com/<OWNER>/<REPO> --token <TOKEN>
   3. Install and start as a service:
        sudo ./svc.sh install
        sudo ./svc.sh start

${CYAN}D) Start the base infrastructure${NC}
   cd ${INFRA_DIR}
   docker compose --env-file .env up -d

   Verify:
   - Traefik dashboard: http://dashboard.\${HOMEBOX_DOMAIN} (or http://<HOST_IP>:8080)

${CYAN}Architecture note:${NC}
   The base infrastructure runs ONLY Traefik. Each project brings its own
   backing services (Postgres, Redis, etc.) in its own docker-compose.yml.
   This means:
   - No port conflicts between projects
   - Each project can pin its own DB/cache versions independently
   - Projects communicate internally via Docker DNS (no host ports needed)
   - Only Traefik exposes ports 80/8080 to the host
   - Projects join the shared "traefik-net" network for HTTP routing
   - Project data is persisted under ${PROJECTS_DIR}/<project-name>/

INSTRUCTIONS

info "Done. Happy shipping!"
