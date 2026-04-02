#!/usr/bin/env bash
# =============================================================================
# Homebox One-Liner Installer (macOS / Linux)
# =============================================================================
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/aleontiev/homebox/main/homebox-infra/install.sh | bash
#
# This script:
#   1. Detects your platform (macOS or Linux)
#   2. Checks for / installs Docker
#   3. Downloads the Homebox provisioner
#   4. Runs the interactive setup
# =============================================================================

set -euo pipefail

REPO_URL="https://github.com/aleontiev/homebox.git"
BRANCH="master"

# ── Colors ───────────────────────────────────────────────────────────────────
if [ -t 1 ]; then
    BOLD='\033[1m' GREEN='\033[0;32m' YELLOW='\033[1;33m'
    CYAN='\033[0;36m' RED='\033[0;31m' NC='\033[0m'
else
    BOLD='' GREEN='' YELLOW='' CYAN='' RED='' NC=''
fi
info()  { printf "${GREEN}[INFO]${NC}  %s\n" "$*"; }
warn()  { printf "${YELLOW}[WARN]${NC}  %s\n" "$*"; }
fail()  { printf "${RED}[FAIL]${NC}  %s\n" "$*" >&2; exit 1; }

# ── Banner ───────────────────────────────────────────────────────────────────
printf "\n${BOLD}${CYAN}"
echo ".__                         ___.                 "
echo "|  |__   ____   _____   ____\\_ |__   _______  ___"
echo "|  |  \\ /  _ \\ /     \\_/ __ \\| __ \\ /  _ \\  \\/  /"
echo "|   Y  (  <_> )  Y Y  \\  ___/| \\_\\ (  <_> >    < "
echo "|___|  /\____/|__|_|  /\\___  >___  /\____/__/\\_ \\"
echo "     \\/             \\/     \\/    \\/            \\/"

printf "${NC}\n"
echo "  Self-hosted Internal PaaS — Installer"
echo ""

# ── Platform ─────────────────────────────────────────────────────────────────
case "$(uname -s)" in
    Linux*)   PLATFORM="linux" ;;
    Darwin*)  PLATFORM="macos" ;;
    *)        fail "Unsupported platform: $(uname -s). Use install.ps1 for Windows." ;;
esac
info "Platform: $PLATFORM ($(uname -m))"

# ── Git ──────────────────────────────────────────────────────────────────────
if ! command -v git >/dev/null 2>&1; then
    info "Installing git..."
    case "$PLATFORM" in
        linux) sudo apt-get update -qq && sudo apt-get install -y -qq git ;;
        macos) xcode-select --install 2>/dev/null || true ;;
    esac
fi

# ── Docker ───────────────────────────────────────────────────────────────────
if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
    info "Docker is running: $(docker --version)"
else
    if command -v docker >/dev/null 2>&1; then
        fail "Docker is installed but not running. Start Docker and re-run this installer."
    fi

    info "Docker not found. Installing..."
    case "$PLATFORM" in
        linux)
            curl -fsSL https://get.docker.com | sudo sh
            sudo usermod -aG docker "$USER" 2>/dev/null || true
            info "Docker installed. You may need to log out and back in for group changes."
            ;;
        macos)
            if command -v brew >/dev/null 2>&1; then
                brew install --cask docker
                echo ""
                warn "Docker Desktop has been installed."
                warn "Open Docker Desktop from Applications, wait for it to start,"
                warn "then re-run this installer:"
                warn "  curl -fsSL https://raw.githubusercontent.com/aleontiev/homebox/master/homebox-infra/install.sh | bash"
                exit 0
            else
                fail "Install Docker Desktop from https://docker.com/products/docker-desktop or install Homebrew (https://brew.sh) first."
            fi
            ;;
    esac
fi

# ── Download Homebox ─────────────────────────────────────────────────────────
CLONE_DIR="$(mktemp -d)"
trap 'rm -rf "$CLONE_DIR"' EXIT

info "Downloading Homebox..."
git clone --depth 1 --branch "$BRANCH" "$REPO_URL" "$CLONE_DIR" 2>/dev/null

# ── Run provisioner ──────────────────────────────────────────────────────────
info "Running host provisioner..."
echo ""

PROVISIONER="$CLONE_DIR/homebox-infra/host-provisioner/setup_host.sh"
chmod +x "$PROVISIONER"
chmod +x "$CLONE_DIR/homebox-infra/host-provisioner/lib.sh"
chmod +x "$CLONE_DIR/homebox-infra/host-provisioner/configure.sh"

if [ "$PLATFORM" = "linux" ]; then
    sudo bash "$PROVISIONER"
else
    bash "$PROVISIONER"
fi
