#!/usr/bin/env bash
# =============================================================================
# Homebox — shared utilities for provisioner scripts
# =============================================================================
# Sourced by setup_host.sh, configure.sh, and install.sh.
# Must be compatible with bash 3.2+ (macOS default).
# =============================================================================

# ── Colors (disabled when stdout is not a terminal) ──────────────────────────
if [ -t 1 ]; then
    BOLD='\033[1m'
    GREEN='\033[0;32m'
    YELLOW='\033[1;33m'
    CYAN='\033[0;36m'
    RED='\033[0;31m'
    NC='\033[0m'
else
    BOLD='' GREEN='' YELLOW='' CYAN='' RED='' NC=''
fi

info()    { printf "${GREEN}[INFO]${NC}  %s\n" "$*"; }
warn()    { printf "${YELLOW}[WARN]${NC}  %s\n" "$*"; }
step()    { printf "\n${CYAN}${BOLD}── %s ──${NC}\n\n" "$*"; }
fail()    { printf "${RED}[FAIL]${NC}  %s\n" "$*" >&2; exit 1; }
success() { printf "${GREEN}[DONE]${NC}  %s\n" "$*"; }

# ── Banner ───────────────────────────────────────────────────────────────────
banner() {
    printf "\n${BOLD}${CYAN}"
    echo '  .__                         ___.                 '
    echo '  |  |__   ____   _____   ____\_ |__   _______  ___'
    echo '  |  |  \ /  _ \ /     \_/ __ \| __ \ /  _ \  \/  /'
    echo '  |   Y  (  <_> )  Y Y  \  ___/| \_\ (  <_> >    < '
    echo '  |___|  /\____/|__|_|  /\___  >___  /\____/__/\_ \'
    echo '       \/             \/     \/    \/            \/'
    printf "${NC}\n"
    echo "  Self-hosted Internal PaaS"
    echo ""
}

# ── Platform detection ───────────────────────────────────────────────────────
detect_platform() {
    case "$(uname -s)" in
        Linux*)   PLATFORM="linux" ;;
        Darwin*)  PLATFORM="macos" ;;
        MINGW*|MSYS*|CYGWIN*)
            fail "Windows detected. Use install.ps1 instead (irm ... | iex)." ;;
        *)
            fail "Unsupported platform: $(uname -s)" ;;
    esac

    # Architecture
    case "$(uname -m)" in
        x86_64)        ARCH="amd64"; RUNNER_ARCH="x64" ;;
        aarch64|arm64) ARCH="arm64"; RUNNER_ARCH="arm64" ;;
        *)             fail "Unsupported architecture: $(uname -m)" ;;
    esac
}

# ── Platform-appropriate base directories ────────────────────────────────────
set_base_dirs() {
    if [ "$PLATFORM" = "macos" ]; then
        HOMEBOX_BASE_DIR="${HOMEBOX_BASE_DIR:-$HOME/homebox}"
    else
        HOMEBOX_BASE_DIR="${HOMEBOX_BASE_DIR:-/opt/homebox}"
    fi
    HOMEBOX_TRAEFIK_DIR="${HOMEBOX_BASE_DIR}/traefik"
    HOMEBOX_PROJECTS_DIR="${HOMEBOX_BASE_DIR}/projects"
    HOMEBOX_INFRA_DIR="${HOMEBOX_BASE_DIR}/base-infrastructure"
}

# ── Docker check ─────────────────────────────────────────────────────────────
require_docker() {
    if ! command -v docker >/dev/null 2>&1; then
        fail "Docker is not installed. Install Docker first, then re-run this script."
    fi
    if ! docker info >/dev/null 2>&1; then
        fail "Docker is installed but not running. Start Docker and re-run this script."
    fi
}

# ── Terminal helpers ────────────────────────────────────────────────────────
# A controlling terminal may still exist when stdin is piped, e.g. curl | bash.
has_tty() {
    [ -t 0 ] || [ -t 1 ] || [ -t 2 ] || { [ -r /dev/tty ] && [ -w /dev/tty ]; }
}

# Run an interactive command against the controlling terminal when stdin is
# redirected, while preserving normal execution for real TTY sessions.
function run_with_tty {
    if [ -t 0 ] || ! { [ -r /dev/tty ] && [ -w /dev/tty ]; }; then
        "$@"
    else
        "$@" </dev/tty >/dev/tty 2>&1
    fi
}

# ── Prompt helpers (work even when script is piped via curl) ─────────────────
# Usage: prompt_value "Enter domain" "example.com"
#   → reads from /dev/tty if stdin is not a terminal
prompt_value() {
    local prompt_text="$1"
    local default="${2:-}"
    local result

    if [ -n "$default" ]; then
        prompt_text="$prompt_text [$default]"
    fi

    # Callers use VAR="$(prompt_value ...)", which captures stdout. Always
    # write the prompt to the terminal so it is visible immediately.
    if [ -r /dev/tty ] && [ -w /dev/tty ]; then
        printf "%s: " "$prompt_text" >/dev/tty
        read -r result </dev/tty
    else
        printf "%s: " "$prompt_text" >&2
        read -r result
    fi

    if [ -z "$result" ] && [ -n "$default" ]; then
        result="$default"
    fi
    echo "$result"
}

# Usage: prompt_secret "Enter password"
#   → reads from /dev/tty, hides input
prompt_secret() {
    local prompt_text="$1"
    local result

    # Same capture-safe pattern as prompt_value.
    if [ -r /dev/tty ] && [ -w /dev/tty ]; then
        printf "%s: " "$prompt_text" >/dev/tty
        read -rs result </dev/tty
        echo "" >/dev/tty
    else
        printf "%s: " "$prompt_text" >&2
        read -rs result
        echo "" >&2
    fi
    echo "$result"
}

# Usage: prompt_yn "Set up runner?" → returns 0 (yes) or 1 (no)
prompt_yn() {
    local prompt_text="$1"
    local default="${2:-n}"
    local hint result

    if [ "$default" = "y" ]; then
        hint="[Y/n]"
    else
        hint="[y/N]"
    fi

    result="$(prompt_value "$prompt_text $hint" "")"
    result="$(echo "$result" | tr '[:upper:]' '[:lower:]')"

    if [ -z "$result" ]; then
        result="$default"
    fi

    case "$result" in
        y|yes) return 0 ;;
        *)     return 1 ;;
    esac
}

prompt_existing_action() {
    local subject="$1"
    local default="${2:-keep}"
    local result

    while true; do
        result="$(prompt_value "$subject already exists. Choose action: keep, reinstall, or cancel" "$default")"
        result="$(echo "$result" | tr '[:upper:]' '[:lower:]')"

        case "$result" in
            keep|reinstall|cancel)
                echo "$result"
                return 0
                ;;
            *)
                warn "Please enter keep, reinstall, or cancel."
                ;;
        esac
    done
}

# ── Random secrets ───────────────────────────────────────────────────────────
generate_random_password() {
    LC_ALL=C tr -dc 'A-Za-z0-9_-' </dev/urandom | head -c 24
    echo ""
}

generate_random_hex() {
    local bytes="${1:-32}"
    LC_ALL=C tr -dc 'a-f0-9' </dev/urandom | head -c "$((bytes * 2))"
    echo ""
}

# ── User home (works under sudo) ─────────────────────────────────────────────
homebox_user() {
    if [ -n "${SUDO_USER:-}" ]; then
        echo "$SUDO_USER"
    else
        id -un
    fi
}

homebox_home() {
    if [ -n "${SUDO_USER:-}" ]; then
        getent passwd "$SUDO_USER" 2>/dev/null | cut -d: -f6
    else
        echo "$HOME"
    fi
}

homebox_secrets_dir() {
    echo "$(homebox_home)/.homebox"
}

homebox_secrets_file() {
    echo "$(homebox_secrets_dir)/secrets.json"
}

# Ensure the secrets directory exists, owned by the invoking (sudo) user.
ensure_secrets_dir() {
    local dir
    dir="$(homebox_secrets_dir)"
    if [ ! -d "$dir" ]; then
        mkdir -p "$dir"
        chown "$(homebox_user)" "$dir" 2>/dev/null || true
        chmod 700 "$dir"
    fi
}

# Read a JSON value: read_secret <jq-path>  (e.g. .admin.password)
read_secret() {
    local path="$1"
    local file
    file="$(homebox_secrets_file)"
    [ -f "$file" ] || { echo ""; return 0; }
    if command -v jq >/dev/null 2>&1; then
        jq -r "$path // empty" "$file" 2>/dev/null
    else
        # crude fallback: only supports .a.b.c paths to string scalars
        local key
        key="$(echo "$path" | awk -F. '{print $NF}')"
        sed -n "s/.*\"${key}\"[[:space:]]*:[[:space:]]*\"\\([^\"]*\\)\".*/\\1/p" "$file" | head -1
    fi
}

# Read the whitelisted login emails (one per line) from secrets.json's
# `identities` array. Requires jq; without it, returns nothing (best-effort —
# arrays aren't reliably parseable with the sed fallback).
read_identities() {
    local file
    file="$(homebox_secrets_file)"
    [ -f "$file" ] || return 0
    if command -v jq >/dev/null 2>&1; then
        jq -r '.identities[]? // empty' "$file" 2>/dev/null
    fi
}

# Write the entire secrets.json from a heredoc / stdin. Caller is responsible
# for the JSON content.
write_secrets_json() {
    local file
    ensure_secrets_dir
    file="$(homebox_secrets_file)"
    local tmp="${file}.tmp"
    cat > "$tmp"
    mv "$tmp" "$file"
    chown "$(homebox_user)" "$file" 2>/dev/null || true
    chmod 600 "$file"
}

# ── Boot auto-start (systemd) ─────────────────────────────────────────────────
# Install + enable a systemd unit that brings the whole Homebox stack up in
# order on boot. Needed because Docker Desktop / WSL has no docker.service to
# order against and container restart policies alone don't reliably recover the
# stack after a reboot. Linux + systemd-as-PID1 only; a quiet no-op elsewhere.
install_boot_unit() {
    if [ "$PLATFORM" != "linux" ]; then
        info "Boot unit: skipped (auto-start is Linux/systemd only)."
        return 0
    fi
    if ! command -v systemctl >/dev/null 2>&1 || [ "$(ps -p 1 -o comm= 2>/dev/null)" != "systemd" ]; then
        warn "Boot unit: systemd is not PID 1 — skipping auto-start install."
        warn "  Homebox will still rely on container restart policies; for reliable"
        warn "  boot, enable systemd in WSL (/etc/wsl.conf: [boot] systemd=true)."
        return 0
    fi
    if [ "$(id -u)" -ne 0 ]; then
        warn "Boot unit: needs root to install — re-run with sudo (or 'make enable-boot')."
        return 0
    fi

    local src_dir dest_dir unit
    src_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"   # host-provisioner/ in the repo
    dest_dir="${HOMEBOX_BASE_DIR}/host-provisioner"
    unit="/etc/systemd/system/homebox.service"

    mkdir -p "$dest_dir"
    cp "$src_dir/homebox-boot.sh" "$dest_dir/homebox-boot.sh"
    chmod +x "$dest_dir/homebox-boot.sh"

    sed "s#__HOMEBOX_BASE_DIR__#${HOMEBOX_BASE_DIR}#g" \
        "$src_dir/homebox.service" > "$unit"

    systemctl daemon-reload
    if systemctl enable homebox.service >/dev/null 2>&1; then
        info "Boot unit installed + enabled: $unit (runs $dest_dir/homebox-boot.sh on boot)."
    else
        warn "Boot unit written to $unit but 'systemctl enable' failed."
    fi
}

# ── Initialize ───────────────────────────────────────────────────────────────
detect_platform
set_base_dirs
