#!/usr/bin/env bash
# =============================================================================
# Homebox Host Provisioner
# =============================================================================
# Sets up a machine (Linux or macOS) as a Homebox host: installs Docker,
# creates directories, deploys base infrastructure, and runs interactive
# configuration (domain, auth, cloudflared, GitHub runner).
#
# Usage:
#   Linux:  sudo ./setup_host.sh
#   macOS:  ./setup_host.sh        (no sudo required)
#
# Or via the one-liner installer:
#   curl -fsSL https://raw.githubusercontent.com/.../install.sh | bash
# =============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/lib.sh"

banner

# ─────────────────────────────────────────────────────────────────────────────
# Pre-flight checks
# ─────────────────────────────────────────────────────────────────────────────
if [ "$PLATFORM" = "linux" ] && [ "$(id -u)" -ne 0 ]; then
    fail "On Linux, this script must be run as root (use sudo)."
fi

info "Platform: $PLATFORM ($ARCH)"
info "Base directory: $HOMEBOX_BASE_DIR"

# ─────────────────────────────────────────────────────────────────────────────
# 1. Install Docker
# ─────────────────────────────────────────────────────────────────────────────
step "1/4  Docker"

if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
    info "Docker is running: $(docker --version)"
else
    if command -v docker >/dev/null 2>&1; then
        fail "Docker is installed but not running. Start Docker and re-run this script."
    fi

    info "Docker not found. Installing..."
    case "$PLATFORM" in
        linux)
            curl -fsSL https://get.docker.com | sh
            if [ -n "${SUDO_USER:-}" ]; then
                usermod -aG docker "$SUDO_USER" 2>/dev/null || true
                info "Added $SUDO_USER to the docker group (re-login to take effect)."
            fi
            ;;
        macos)
            if command -v brew >/dev/null 2>&1; then
                info "Installing Docker Desktop via Homebrew..."
                brew install --cask docker
                echo ""
                warn "Docker Desktop has been installed."
                warn "Open Docker Desktop from Applications, wait for it to start,"
                warn "then re-run this script."
                exit 0
            else
                fail "Install Docker Desktop from https://docker.com/products/docker-desktop or install Homebrew (https://brew.sh) first."
            fi
            ;;
    esac
fi

# Verify Docker Compose plugin
if docker compose version >/dev/null 2>&1; then
    info "Docker Compose: $(docker compose version --short)"
else
    if [ "$PLATFORM" = "linux" ]; then
        warn "Docker Compose plugin not found — installing..."
        apt-get update -qq && apt-get install -y -qq docker-compose-plugin
        info "Docker Compose installed: $(docker compose version --short)"
    else
        fail "Docker Compose not available. Ensure Docker Desktop is up to date."
    fi
fi

# ─────────────────────────────────────────────────────────────────────────────
# 2. Create directory layout
# ─────────────────────────────────────────────────────────────────────────────
step "2/4  Creating directories"

mkdir -p "$HOMEBOX_TRAEFIK_DIR" "$HOMEBOX_PROJECTS_DIR" "$HOMEBOX_INFRA_DIR"
info "Directories created under $HOMEBOX_BASE_DIR"

# ─────────────────────────────────────────────────────────────────────────────
# 3. Deploy base infrastructure files
# ─────────────────────────────────────────────────────────────────────────────
step "3/4  Deploying base infrastructure"

SRC_INFRA="${SCRIPT_DIR}/base-infrastructure"

if [ ! -d "$SRC_INFRA" ]; then
    fail "Cannot find base-infrastructure/ next to this script (looked in ${SRC_INFRA})."
fi

cp "$SRC_INFRA/docker-compose.yml" "$HOMEBOX_INFRA_DIR/docker-compose.yml"
cp "$SRC_INFRA/.env.example"       "$HOMEBOX_INFRA_DIR/.env.example"
cp "$SRC_INFRA/dynamic_conf.yml"   "$HOMEBOX_TRAEFIK_DIR/dynamic_conf.yml"

info "Compose file:   ${HOMEBOX_INFRA_DIR}/docker-compose.yml"
info "Dynamic config: ${HOMEBOX_TRAEFIK_DIR}/dynamic_conf.yml"

# ─────────────────────────────────────────────────────────────────────────────
# 4. Create Docker network
# ─────────────────────────────────────────────────────────────────────────────
step "4/4  Docker network"

if docker network inspect traefik-net >/dev/null 2>&1; then
    info "Network traefik-net already exists."
else
    docker network create traefik-net
    info "Created network traefik-net."
fi

# ─────────────────────────────────────────────────────────────────────────────
# Hand off to interactive configuration
# ─────────────────────────────────────────────────────────────────────────────
echo ""
success "Base infrastructure is ready."
echo ""

if prompt_yn "Run interactive configuration now? (domain, auth, tunnel, runner)"; then
    bash "$SCRIPT_DIR/configure.sh"
else
    echo ""
    info "You can run configuration later with:"
    info "  bash $SCRIPT_DIR/configure.sh"
    echo ""
fi
