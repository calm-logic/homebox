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

copy_infra_file() {
    local src="$1"
    local dest="$2"
    local label="$3"
    local action="${4:-reinstall}"

    if [ "$action" = "reinstall" ] || [ ! -f "$dest" ]; then
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

# Address pools: docker's defaults fall back to 192.168.0.0/16 once the
# 172.16/12 space is exhausted, and a network landing there shadows typical
# home LANs — containers then can't reach LAN peers (breaks clustering).
# On native Linux we can fix the daemon config ourselves; Docker Desktop
# users manage this in Settings → Docker Engine, so we can only warn.
if [ "$PLATFORM" = "linux" ] && ! docker info 2>/dev/null | grep -qi "desktop"; then
    DAEMON_JSON=/etc/docker/daemon.json
    if [ ! -s "$DAEMON_JSON" ]; then
        info "Setting docker default-address-pools to 10.201.0.0/16 (avoids LAN-colliding 192.168.x auto-allocation)"
        mkdir -p /etc/docker
        printf '{\n  "default-address-pools": [{"base": "10.201.0.0/16", "size": 24}]\n}\n' > "$DAEMON_JSON"
        systemctl restart docker 2>/dev/null || service docker restart 2>/dev/null || true
    elif ! grep -q "default-address-pools" "$DAEMON_JSON"; then
        warn "$DAEMON_JSON exists without default-address-pools — docker may auto-allocate"
        warn "networks in 192.168.0.0/16 that shadow your LAN. Consider adding:"
        warn '  "default-address-pools": [{"base": "10.201.0.0/16", "size": 24}]'
    fi
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

# Start pulling the images the install needs (infra + prebuilt admin) in the
# background NOW, so the downloads overlap with the rest of setup and
# configure.sh's `docker compose pull app` hits a warm cache. Best-effort:
# offline installs still work via configure.sh's build-from-source fallback.
# shellcheck disable=SC2046  # word-splitting the image list is intended
prepull_images_bg "infra + admin images" $(homebox_infra_images) "$(homebox_admin_image)"

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

infra_action="reinstall"
if [ -f "$HOMEBOX_INFRA_DIR/docker-compose.yml" ] || [ -f "$HOMEBOX_INFRA_DIR/.env.example" ] || [ -f "$HOMEBOX_TRAEFIK_DIR/dynamic_conf.yml" ]; then
    infra_action="$(prompt_existing_action "Base infrastructure files")"
    case "$infra_action" in
        cancel)
            warn "Installation cancelled."
            exit 0
            ;;
        keep)
            info "Keeping existing base infrastructure files where present."
            ;;
    esac
fi

copy_infra_file "$SRC_INFRA/docker-compose.yml" "$HOMEBOX_INFRA_DIR/docker-compose.yml" "Compose file" "$infra_action"
copy_infra_file "$SRC_INFRA/.env.example" "$HOMEBOX_INFRA_DIR/.env.example" "Example env" "$infra_action"
copy_infra_file "$SRC_INFRA/dynamic_conf.yml" "$HOMEBOX_TRAEFIK_DIR/dynamic_conf.yml" "Dynamic config" "$infra_action"

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

info "Bringing up the Homebox admin UI..."
bash "$SCRIPT_DIR/configure.sh"
