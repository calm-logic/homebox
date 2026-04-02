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

if [ "${HOMEBOX_SUPPRESS_BANNER:-0}" != "1" ]; then
    banner
fi

# ─────────────────────────────────────────────────────────────────────────────
# Pre-flight checks
# ─────────────────────────────────────────────────────────────────────────────
if [ "$PLATFORM" = "linux" ] && [ "$(id -u)" -ne 0 ]; then
    fail "On Linux, this script must be run as root (use sudo)."
fi

info "Platform: $PLATFORM ($ARCH)"
info "Base directory: $HOMEBOX_BASE_DIR"

HOMEBOX_INSTALL_MODE="$(echo "${HOMEBOX_INSTALL_MODE:-}" | tr '[:upper:]' '[:lower:]')"
existing_components=()
[ -f "$HOMEBOX_INFRA_DIR/.env" ] && existing_components+=("environment")
[ -f "$HOMEBOX_TRAEFIK_DIR/dynamic_conf.yml" ] && existing_components+=("traefik")
[ -f "$HOMEBOX_BASE_DIR/actions-runner/.runner" ] && existing_components+=("runner")
[ -f "$HOME/.cloudflared/config.yml" ] && existing_components+=("cloudflare")

if [ "${#existing_components[@]}" -gt 0 ] && [ -z "$HOMEBOX_INSTALL_MODE" ]; then
    warn "Existing Homebox setup detected."
    info "Found: ${existing_components[*]}"

    while true; do
        HOMEBOX_INSTALL_MODE="$(prompt_value "Choose action: proceed, reinstall, or cancel" "proceed")"
        HOMEBOX_INSTALL_MODE="$(echo "$HOMEBOX_INSTALL_MODE" | tr '[:upper:]' '[:lower:]')"

        case "$HOMEBOX_INSTALL_MODE" in
            proceed|reinstall) break ;;
            cancel)
                warn "Installation cancelled."
                exit 0
                ;;
            *)
                warn "Please enter proceed, reinstall, or cancel."
                ;;
        esac
    done
fi

if [ -z "$HOMEBOX_INSTALL_MODE" ]; then
    HOMEBOX_INSTALL_MODE="proceed"
fi

export HOMEBOX_INSTALL_MODE
info "Install mode: $HOMEBOX_INSTALL_MODE"

copy_infra_file() {
    local src="$1"
    local dest="$2"
    local label="$3"

    if [ "$HOMEBOX_INSTALL_MODE" = "reinstall" ] || [ ! -f "$dest" ]; then
        cp "$src" "$dest"
        info "$label: ${dest}"
    else
        info "Keeping existing $label: ${dest}"
    fi
}

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

copy_infra_file "$SRC_INFRA/docker-compose.yml" "$HOMEBOX_INFRA_DIR/docker-compose.yml" "Compose file"
copy_infra_file "$SRC_INFRA/.env.example" "$HOMEBOX_INFRA_DIR/.env.example" "Example env"
copy_infra_file "$SRC_INFRA/dynamic_conf.yml" "$HOMEBOX_TRAEFIK_DIR/dynamic_conf.yml" "Dynamic config"

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
