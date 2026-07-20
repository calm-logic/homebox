#!/usr/bin/env bash
# =============================================================================
# Homebox One-Liner Installer (macOS / Linux)
# =============================================================================
# Usage:
#   curl -fsSL https://homebox.sh/install.sh | bash
#   # (mirror: https://raw.githubusercontent.com/calm-logic/homebox/master/host/install.sh)
#
#   # Uninstall (interactive confirmation; keeps data volumes + secrets):
#   curl -fsSL https://homebox.sh/install.sh | bash -s -- --uninstall
#   # Uninstall everything, no questions asked (volumes, secrets, images too):
#   curl -fsSL https://homebox.sh/install.sh | bash -s -- --uninstall --yes --purge
#
# Options:
#   --uninstall   remove Homebox from this machine instead of installing
#   --yes         skip the uninstall confirmation prompt (REQUIRED when piped
#                 without a terminal)
#   --purge       also delete docker volumes (databases!), ~/.homebox secrets,
#                 and Homebox docker images. Without it, volumes + secrets are
#                 kept so a reinstall can adopt them.
#
# Environment:
#   HOMEBOX_NO_BROWSER=1   skip the automatic browser open at the end
#   HOMEBOX_BASE_DIR       override the base dir (default /opt/homebox on
#                          Linux/WSL, ~/homebox on macOS) — honored by both
#                          install and uninstall
#
# This script:
#   1. Detects your platform (macOS or Linux, including WSL2)
#   2. Checks for / installs Docker
#   3. Downloads the Homebox provisioner
#   4. Runs the interactive setup
#   5. Opens the admin UI (http://localhost:7765) when done
# =============================================================================

set -euo pipefail

REPO_URL="https://github.com/calm-logic/homebox.git"
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

# ── Arguments ────────────────────────────────────────────────────────────────
UNINSTALL=0
ASSUME_YES=0
PURGE=0
for arg in "$@"; do
    case "$arg" in
        --uninstall) UNINSTALL=1 ;;
        --yes|-y)    ASSUME_YES=1 ;;
        --purge)     PURGE=1 ;;
        --help|-h)
            echo "Usage: install.sh [--uninstall [--yes] [--purge]]"
            echo "  (no flags)    install Homebox"
            echo "  --uninstall   remove Homebox from this machine"
            echo "  --yes         skip the uninstall confirmation prompt"
            echo "  --purge       also delete volumes, ~/.homebox secrets, and images"
            exit 0
            ;;
        *) fail "Unknown option: $arg (try --help)" ;;
    esac
done
if [ "$UNINSTALL" != "1" ] && { [ "$ASSUME_YES" = "1" ] || [ "$PURGE" = "1" ]; }; then
    fail "--yes/--purge only apply together with --uninstall."
fi

# ── Banner ───────────────────────────────────────────────────────────────────
printf "\n${BOLD}${CYAN}"
echo ".__                         ___.                 "
echo "|  |__   ____   _____   ____\\_ |__   _______  ___"
echo "|  |  \\ /  _ \\ /     \\_/ __ \\| __ \\ /  _ \\  \\/  /"
echo "|   Y  (  <_> )  Y Y  \\  ___/| \\_\\ (  <_> >    < "
echo "|___|  /\____/|__|_|  /\\___  >___  /\____/__/\\_ \\"
echo "     \\/             \\/     \\/    \\/            \\/"

printf "${NC}\n"
if [ "$UNINSTALL" = "1" ]; then
    echo "  Self-hosted Internal PaaS — Uninstaller"
else
    echo "  Self-hosted Internal PaaS — Installer"
fi
echo ""

# ── Platform ─────────────────────────────────────────────────────────────────
case "$(uname -s)" in
    Linux*)   PLATFORM="linux" ;;
    Darwin*)  PLATFORM="macos" ;;
    *)        fail "Unsupported platform: $(uname -s). Use install.ps1 for Windows." ;;
esac
info "Platform: $PLATFORM ($(uname -m))"

# ── WSL detection ────────────────────────────────────────────────────────────
IS_WSL=0
if [ "$PLATFORM" = "linux" ] && grep -qi microsoft /proc/version 2>/dev/null; then
    IS_WSL=1
    info "Running inside WSL (Windows Subsystem for Linux)."
fi

# =============================================================================
# Uninstall
# =============================================================================
# Self-contained on purpose: no repo clone, no lib.sh — it must work on a box
# where only the installed artifacts remain. Mirrors what setup_host.sh /
# configure.sh create (see host/host-provisioner/ in the repo).

# A controlling terminal may still exist when stdin is piped (curl | bash).
# Permission bits on /dev/tty are not enough — actually try to open it.
tty_usable() {
    ( : </dev/tty && : >/dev/tty ) 2>/dev/null
}

# Run a command as root when we aren't already (and sudo exists).
maybe_sudo() {
    if [ "$(id -u)" -eq 0 ] || ! command -v sudo >/dev/null 2>&1; then
        "$@"
    else
        sudo "$@"
    fi
}

# Read a string value out of secrets.json: ujson_secret <jq-path> <file>
ujson_secret() {
    local path="$1" file="$2"
    [ -f "$file" ] || { echo ""; return 0; }
    if command -v jq >/dev/null 2>&1; then
        jq -r "$path // empty" "$file" 2>/dev/null || true
    else
        # crude fallback: only supports .a.b paths to string scalars
        local key
        key="$(echo "$path" | awk -F. '{print $NF}')"
        sed -n "s/.*\"${key}\"[[:space:]]*:[[:space:]]*\"\\([^\"]*\\)\".*/\\1/p" "$file" | head -1
    fi
}

# Best-effort cloud deregistration: if the admin is still running locally AND
# we can log in, ask it to leave its cluster so the node doesn't linger in the
# account's god view. Never fatal — every failure just prints a note.
uninstall_cloud_dereg() {
    local secrets_file="$1"
    local port="${HOMEBOX_ADMIN_PORT:-7765}"
    local base="http://127.0.0.1:${port}"

    if ! command -v curl >/dev/null 2>&1; then
        warn "curl not found — skipping cluster deregistration."
        return 0
    fi
    if ! curl -fsS -m 4 -o /dev/null "$base/api/auth/login-options" 2>/dev/null; then
        info "Admin API not reachable on $base — skipping cluster deregistration."
        return 0
    fi

    local username plain
    username="$(ujson_secret '.admin.username' "$secrets_file")"
    [ -n "$username" ] || username="homebox"
    # configure.sh persists ONLY a bcrypt hash (.admin.password_hash) — a
    # plaintext .admin.password normally doesn't exist. Cloud-mirror installs
    # do keep the plaintext next door (mirror-admin-password), so try both.
    plain="$(ujson_secret '.admin.password' "$secrets_file")"
    if [ -z "$plain" ] && [ -f "$(dirname "$secrets_file")/mirror-admin-password" ]; then
        plain="$(cat "$(dirname "$secrets_file")/mirror-admin-password" 2>/dev/null || true)"
    fi
    if [ -z "$plain" ]; then
        warn "secrets.json stores only a bcrypt password hash (no plaintext) —"
        warn "cannot log in to the admin API for automatic cluster deregistration."
        warn "If this node was in a cluster it may linger in your account's node"
        warn "list — evict it from the portal or another node's Cluster page."
        return 0
    fi

    local jar body code
    jar="$(mktemp)"
    # Escape backslashes + quotes for the JSON body (generated passwords are
    # [A-Za-z0-9_-], but user-set ones may not be).
    body="$(printf '{"username":"%s","password":"%s"}' \
        "$(printf '%s' "$username" | sed 's/\\/\\\\/g; s/"/\\"/g')" \
        "$(printf '%s' "$plain"    | sed 's/\\/\\\\/g; s/"/\\"/g')")"
    code="$(curl -sS -m 8 -o /dev/null -w '%{http_code}' -c "$jar" \
        -H 'Content-Type: application/json' -d "$body" \
        "$base/api/auth/login" 2>/dev/null || echo 000)"
    if [ "$code" != "200" ]; then
        warn "Admin API login failed (HTTP $code) — skipping cluster deregistration."
        rm -f "$jar"
        return 0
    fi

    code="$(curl -sS -m 20 -o /dev/null -w '%{http_code}' -b "$jar" -X POST \
        -H 'Content-Type: application/json' \
        -d '{"stop_tunnel": true, "teardown_stacks": false}' \
        "$base/api/cluster/leave" 2>/dev/null || echo 000)"
    rm -f "$jar"
    case "$code" in
        200) info "Cluster deregistration: this node left its cluster." ;;
        404) info "Cluster deregistration: node was not part of a cluster — nothing to do." ;;
        *)   warn "Cluster deregistration returned HTTP $code — continuing anyway."
             warn "If the node lingers in your account, evict it from the portal." ;;
    esac
}

run_uninstall() {
    # Base dirs (mirrors host-provisioner/lib.sh:set_base_dirs).
    local base_dir secrets_dir home_dir
    if [ "$PLATFORM" = "macos" ]; then
        base_dir="${HOMEBOX_BASE_DIR:-$HOME/homebox}"
    else
        base_dir="${HOMEBOX_BASE_DIR:-/opt/homebox}"
    fi
    home_dir="$HOME"
    if [ -n "${SUDO_USER:-}" ]; then
        local sudo_home
        sudo_home="$(getent passwd "$SUDO_USER" 2>/dev/null | cut -d: -f6 || true)"
        [ -n "$sudo_home" ] && home_dir="$sudo_home"
    fi
    secrets_dir="$home_dir/.homebox"

    case "$base_dir" in
        ""|"/"|"$home_dir") fail "Refusing to remove suspicious base dir: '$base_dir'" ;;
    esac

    echo "This removes Homebox from this machine:"
    echo "  - all Homebox app stacks (homebox-proj-*), the admin stack, and Traefik"
    echo "  - the cloudflared connector container and the traefik-net docker network"
    echo "  - the systemd boot unit (if installed)"
    echo "  - the base directory: $base_dir"
    if [ "$PURGE" = "1" ]; then
        echo "  - [--purge] named docker volumes — INCLUDING ALL DATABASES"
        echo "  - [--purge] $secrets_dir (admin password hash, encryption secrets)"
        echo "  - [--purge] Homebox docker images"
    else
        echo "Kept (reinstall adopts them; pass --purge to remove):"
        echo "  - docker volumes (admin + project databases)"
        echo "  - $secrets_dir (admin credentials) and the admin's .env secrets"
    fi
    echo ""

    # Confirmation: interactive prompt via /dev/tty; a pipe without a terminal
    # must be explicit with --yes.
    if [ "$ASSUME_YES" != "1" ]; then
        local answer=""
        if [ -t 0 ]; then
            printf "Proceed with uninstall? [y/N]: "
            read -r answer || true
        elif tty_usable; then
            printf "Proceed with uninstall? [y/N]: " >/dev/tty
            read -r answer </dev/tty || true
        else
            fail "No terminal available for confirmation. Re-run with --yes to uninstall non-interactively."
        fi
        case "$(echo "$answer" | tr '[:upper:]' '[:lower:]')" in
            y|yes) ;;
            *) info "Uninstall cancelled — nothing was changed."; exit 0 ;;
        esac
    fi
    echo ""

    # ── 1. Cloud deregistration (best-effort, before the admin goes down) ────
    uninstall_cloud_dereg "$secrets_dir/secrets.json"

    # ── 2. Docker teardown ────────────────────────────────────────────────────
    local docker_ok=0 DOCKER="docker"
    if command -v docker >/dev/null 2>&1; then
        if docker info >/dev/null 2>&1; then
            docker_ok=1
        elif maybe_sudo docker info >/dev/null 2>&1; then
            docker_ok=1
            DOCKER="maybe_sudo docker"
        fi
    fi

    local down_flags="--remove-orphans"
    [ "$PURGE" = "1" ] && down_flags="$down_flags --volumes"

    if [ "$docker_ok" = "1" ]; then
        # Enumerate compose projects via labels; a Homebox stack is either a
        # homebox-proj-* project or one whose compose working_dir lives under
        # the base dir (admin, base-infrastructure).
        local listing selected="" proj wd
        listing="$($DOCKER ps -a --filter label=com.docker.compose.project \
            --format '{{.Label "com.docker.compose.project"}}|{{.Label "com.docker.compose.project.working_dir"}}' \
            2>/dev/null | sort -u || true)"
        if [ -n "$listing" ]; then
            while IFS='|' read -r proj wd; do
                [ -n "$proj" ] || continue
                case "$proj" in
                    homebox-proj-*) selected="$selected $proj" ;;
                    *)
                        case "$wd" in
                            "$base_dir"|"$base_dir"/*) selected="$selected $proj" ;;
                        esac
                        ;;
                esac
            done <<EOF
$listing
EOF
            selected="$(echo "$selected" | tr ' ' '\n' | sort -u | tr '\n' ' ')"
        else
            # Fallback (label enumeration failed / nothing labeled): only the
            # well-known project names — verified by their signature containers.
            if $DOCKER ps -a --format '{{.Names}}' 2>/dev/null | grep -qx 'homebox-admin'; then
                selected="$selected admin"
            fi
            if $DOCKER ps -a --format '{{.Names}}' 2>/dev/null | grep -qx 'homebox-traefik'; then
                selected="$selected base-infrastructure"
            fi
        fi

        if [ -n "$(echo "$selected" | tr -d ' ')" ]; then
            for proj in $selected; do
                info "Removing compose stack: $proj"
                # compose v2 reconstructs the stack from labels — no compose
                # file needed.
                $DOCKER compose -p "$proj" down $down_flags >/dev/null 2>&1 \
                    || warn "  'docker compose -p $proj down' had errors (continuing)."
            done
        else
            info "No Homebox compose stacks found."
        fi

        # Stray containers (cloudflared is started by the admin with plain
        # `docker run`, so it carries no compose labels).
        local c
        for c in homebox-cloudflared homebox-admin homebox-admin-db \
                 homebox-traefik homebox-docker-proxy; do
            if $DOCKER ps -a --format '{{.Names}}' 2>/dev/null | grep -qx "$c"; then
                info "Removing container: $c"
                $DOCKER rm -f "$c" >/dev/null 2>&1 || warn "  could not remove $c (continuing)."
            fi
        done
        while IFS= read -r c; do
            [ -n "$c" ] || continue
            info "Removing container: $c"
            $DOCKER rm -f "$c" >/dev/null 2>&1 || true
        done <<EOF
$($DOCKER ps -a --format '{{.Names}}' 2>/dev/null | grep '^homebox-proj-' || true)
EOF

        # Network.
        if $DOCKER network inspect traefik-net >/dev/null 2>&1; then
            if $DOCKER network rm traefik-net >/dev/null 2>&1; then
                info "Removed docker network: traefik-net"
            else
                warn "Could not remove network traefik-net (still in use?) — continuing."
            fi
        else
            info "Docker network traefik-net not present."
        fi

        if [ "$PURGE" = "1" ]; then
            # Orphaned named volumes (stacks already gone → `down -v` missed them).
            local vol vproj
            while IFS='|' read -r vol vproj; do
                [ -n "$vol" ] || continue
                case "$vproj" in
                    homebox-proj-*) ;;
                    *)
                        case " $selected " in
                            *" $vproj "*) [ -n "$vproj" ] || continue ;;
                            *) continue ;;
                        esac
                        ;;
                esac
                info "Removing volume: $vol"
                $DOCKER volume rm "$vol" >/dev/null 2>&1 || warn "  could not remove volume $vol."
            done <<EOF
$($DOCKER volume ls --format '{{.Name}}|{{.Label "com.docker.compose.project"}}' 2>/dev/null || true)
EOF

            # Images: prebuilt admin + locally built project images.
            local img
            while IFS= read -r img; do
                [ -n "$img" ] || continue
                info "Removing image: $img"
                $DOCKER rmi -f "$img" >/dev/null 2>&1 || warn "  could not remove image $img."
            done <<EOF
$($DOCKER images --format '{{.Repository}}:{{.Tag}}' 2>/dev/null \
    | grep -E '^(ghcr\.io/calm-logic/homebox-admin:|homebox-proj-)' || true)
EOF
        fi
    else
        warn "Docker is not available/running — skipping container, network, volume,"
        warn "and image cleanup. Re-run with Docker up to finish those, or remove them manually."
    fi

    # ── 3. Boot unit (configure.sh installs /etc/systemd/system/homebox.service)
    local unit="/etc/systemd/system/homebox.service"
    if [ "$PLATFORM" = "linux" ]; then
        if command -v systemctl >/dev/null 2>&1 \
           && [ "$(ps -p 1 -o comm= 2>/dev/null)" = "systemd" ]; then
            if [ -f "$unit" ]; then
                maybe_sudo systemctl disable --now homebox.service >/dev/null 2>&1 || true
                maybe_sudo rm -f "$unit" || warn "Could not remove $unit."
                maybe_sudo systemctl daemon-reload >/dev/null 2>&1 || true
                info "Removed boot unit: homebox.service"
            else
                info "Boot unit not installed — nothing to remove."
            fi
        elif [ -f "$unit" ]; then
            # Unit file exists but systemd isn't PID 1 (e.g. WSL without systemd).
            maybe_sudo rm -f "$unit" || warn "Could not remove $unit."
            info "Removed boot unit file (systemd not running): $unit"
        else
            info "No systemd / no boot unit — skipping."
        fi
    fi

    # ── 4. Base directory ─────────────────────────────────────────────────────
    if [ -d "$base_dir" ]; then
        # Without --purge, keep the admin's .env: it holds DB_PASSWORD and
        # ENCRYPTION_KEY, which the kept admin-db volume + encrypted rows need.
        # A reinstall finds it in place and adopts both.
        local kept_env=""
        if [ "$PURGE" != "1" ] && maybe_sudo test -f "$base_dir/admin/.env"; then
            kept_env="$(mktemp)"
            maybe_sudo cp "$base_dir/admin/.env" "$kept_env" 2>/dev/null || kept_env=""
        fi
        if maybe_sudo rm -rf "$base_dir"; then
            info "Removed base directory: $base_dir"
        else
            warn "Could not fully remove $base_dir — remove it manually."
        fi
        if [ -n "$kept_env" ]; then
            maybe_sudo mkdir -p "$base_dir/admin"
            maybe_sudo cp "$kept_env" "$base_dir/admin/.env"
            maybe_sudo chmod 600 "$base_dir/admin/.env" 2>/dev/null || true
            rm -f "$kept_env"
            info "Kept $base_dir/admin/.env (DB password + encryption key) so a"
            info "reinstall can adopt the kept database volume."
        fi
    else
        info "Base directory $base_dir not present."
    fi

    # ── 5. Secrets ────────────────────────────────────────────────────────────
    if [ "$PURGE" = "1" ]; then
        if [ -d "$secrets_dir" ]; then
            rm -rf "$secrets_dir" 2>/dev/null || maybe_sudo rm -rf "$secrets_dir" || true
            if [ -d "$secrets_dir" ]; then
                warn "Could not remove $secrets_dir — remove it manually."
            else
                info "Removed secrets: $secrets_dir"
            fi
        else
            info "Secrets dir $secrets_dir not present."
        fi
    fi

    # ── Summary ───────────────────────────────────────────────────────────────
    echo ""
    echo "=============================================="
    printf "${GREEN}[DONE]${NC}  Homebox has been uninstalled.\n"
    echo "=============================================="
    if [ "$PURGE" = "1" ]; then
        info "Purge: docker volumes, $secrets_dir, and Homebox images were removed."
    else
        info "Kept for a future reinstall (use --purge to remove):"
        info "  - docker volumes (admin + project databases)"
        info "  - $secrets_dir (admin credentials)"
        info "  - $base_dir/admin/.env (DB password + encryption key), if it existed"
        info "Reinstalling with the same one-liner will adopt them."
    fi
    if [ "$IS_WSL" = "1" ]; then
        info "Docker Desktop and this WSL distro were left untouched."
    fi
}

if [ "$UNINSTALL" = "1" ]; then
    run_uninstall
    exit 0
fi

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
                warn "  curl -fsSL https://raw.githubusercontent.com/calm-logic/homebox/master/host/install.sh | bash"
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

# ── Browser open (WSL) ───────────────────────────────────────────────────────
# Inside WSL `xdg-open` is a no-op, so the provisioner's auto-open is disabled
# (HOMEBOX_NO_BROWSER=1) and the admin UI is opened from the Windows side here
# instead. WSL2 forwards localhost, so http://localhost:7765 works in Windows.
open_admin_from_windows() {
    local url="http://localhost:${HOMEBOX_ADMIN_PORT:-7765}"
    if command -v wslview >/dev/null 2>&1; then
        wslview "$url" >/dev/null 2>&1 || true
        return 0
    fi
    local ps_exe
    ps_exe="$(command -v powershell.exe 2>/dev/null || true)"
    if [ -z "$ps_exe" ] && [ -x "/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe" ]; then
        ps_exe="/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe"
    fi
    if [ -n "$ps_exe" ]; then
        "$ps_exe" -NoProfile -NonInteractive -Command "Start-Process '$url'" >/dev/null 2>&1 || true
    else
        info "Open $url in your browser."
    fi
}

# ── Run provisioner ──────────────────────────────────────────────────────────
info "Running host provisioner..."
echo ""

PROVISIONER="$CLONE_DIR/host/host-provisioner/setup_host.sh"
chmod +x "$PROVISIONER"
chmod +x "$CLONE_DIR/host/host-provisioner/lib.sh"
chmod +x "$CLONE_DIR/host/host-provisioner/configure.sh"

NO_BROWSER="${HOMEBOX_NO_BROWSER:-}"
PROVISIONER_NO_BROWSER="$NO_BROWSER"
if [ "$IS_WSL" = "1" ]; then
    # xdg-open is a no-op in WSL; the open happens from Windows below.
    PROVISIONER_NO_BROWSER=1
fi

if [ "$PLATFORM" = "linux" ]; then
    sudo HOMEBOX_SUPPRESS_BANNER=1 HOMEBOX_NO_BROWSER="$PROVISIONER_NO_BROWSER" bash "$PROVISIONER"
else
    HOMEBOX_SUPPRESS_BANNER=1 HOMEBOX_NO_BROWSER="$PROVISIONER_NO_BROWSER" bash "$PROVISIONER"
fi

if [ "$IS_WSL" = "1" ] && [ "$NO_BROWSER" != "1" ]; then
    open_admin_from_windows
fi
