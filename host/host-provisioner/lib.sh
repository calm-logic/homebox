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

    if [ -t 0 ]; then
        printf "%s: " "$prompt_text"
        read -r result
    else
        printf "%s: " "$prompt_text" >/dev/tty
        read -r result </dev/tty
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

    if [ -t 0 ]; then
        printf "%s: " "$prompt_text"
        read -rs result
        echo ""
    else
        printf "%s: " "$prompt_text" >/dev/tty
        read -rs result </dev/tty
        echo "" >/dev/tty
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

# ── Initialize ───────────────────────────────────────────────────────────────
detect_platform
set_base_dirs
