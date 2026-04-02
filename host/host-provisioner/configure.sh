#!/usr/bin/env bash
# =============================================================================
# Homebox Interactive Configuration
# =============================================================================
# Configures domain, dashboard auth, Cloudflare Tunnel, and GitHub Actions
# runner. Safe to re-run — each step detects existing config and skips or
# offers to reconfigure.
#
# Called automatically by setup_host.sh, or run standalone:
#   bash configure.sh
# =============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/lib.sh"

require_docker

HOMEBOX_INSTALL_MODE="${HOMEBOX_INSTALL_MODE:-proceed}"
HOMEBOX_ENV_NEEDS_WRITE=0

load_existing_env() {
    if [ ! -f "$HOMEBOX_INFRA_DIR/.env" ]; then
        return 0
    fi

    if [ -z "${HOMEBOX_DOMAIN:-}" ]; then
        HOMEBOX_DOMAIN="$(sed -n 's/^HOMEBOX_DOMAIN=//p' "$HOMEBOX_INFRA_DIR/.env" | head -1)"
    fi

    if [ -z "${TRAEFIK_DASHBOARD_AUTH:-}" ]; then
        TRAEFIK_DASHBOARD_AUTH="$(sed -n 's/^TRAEFIK_DASHBOARD_AUTH=//p' "$HOMEBOX_INFRA_DIR/.env" | head -1)"
    fi
}

# ─────────────────────────────────────────────────────────────────────────────
# Step 1: Domain
# ─────────────────────────────────────────────────────────────────────────────
configure_domain() {
    step "Step 1/6 — Domain Configuration"

    if [ "$HOMEBOX_INSTALL_MODE" != "reinstall" ]; then
        load_existing_env
        if [ -n "${HOMEBOX_DOMAIN:-}" ]; then
            info "Keeping existing domain: $HOMEBOX_DOMAIN"
            return 0
        fi
    fi

    HOMEBOX_DOMAIN="$(prompt_value "Enter your root domain (e.g. example.com)" "")"
    HOMEBOX_ENV_NEEDS_WRITE=1

    if [ -z "$HOMEBOX_DOMAIN" ]; then
        fail "Domain cannot be empty."
    fi

    info "Domain: $HOMEBOX_DOMAIN"
    info "Projects will be accessible at <project>.$HOMEBOX_DOMAIN"
}

# ─────────────────────────────────────────────────────────────────────────────
# Step 2: Dashboard authentication
# ─────────────────────────────────────────────────────────────────────────────
configure_dashboard_auth() {
    step "Step 2/6 — Traefik Dashboard Authentication"

    if [ "$HOMEBOX_INSTALL_MODE" != "reinstall" ]; then
        load_existing_env
        if [ -n "${TRAEFIK_DASHBOARD_AUTH:-}" ]; then
            info "Keeping existing dashboard credentials from $HOMEBOX_INFRA_DIR/.env"
            return 0
        fi
    fi

    local username password password2 hash

    username="$(prompt_value "Dashboard username" "admin")"

    while true; do
        password="$(prompt_secret "Dashboard password")"
        if [ -z "$password" ]; then
            warn "Password cannot be empty."
            continue
        fi
        password2="$(prompt_secret "Confirm password")"
        if [ "$password" = "$password2" ]; then
            break
        fi
        warn "Passwords do not match. Try again."
    done

    # Generate htpasswd hash using Docker (no binary dependency)
    info "Generating credentials..."
    hash="$(docker run --rm httpd:2-alpine htpasswd -nb "$username" "$password" 2>/dev/null)"

    if [ -z "$hash" ]; then
        fail "Failed to generate htpasswd hash."
    fi

    # Escape $ for docker-compose .env format ($ → $$)
    TRAEFIK_DASHBOARD_AUTH="$(echo "$hash" | sed 's/\$/\$\$/g')"
    HOMEBOX_ENV_NEEDS_WRITE=1

    info "Dashboard credentials generated for user: $username"
}

# ─────────────────────────────────────────────────────────────────────────────
# Step 3: Generate .env
# ─────────────────────────────────────────────────────────────────────────────
generate_env() {
    step "Step 3/6 — Generating .env"

    if [ "$HOMEBOX_INSTALL_MODE" != "reinstall" ] && [ "$HOMEBOX_ENV_NEEDS_WRITE" -eq 0 ] && [ -f "$HOMEBOX_INFRA_DIR/.env" ]; then
        info "Keeping existing environment file: $HOMEBOX_INFRA_DIR/.env"
        return 0
    fi

    cat > "$HOMEBOX_INFRA_DIR/.env" <<EOF
HOMEBOX_DOMAIN=${HOMEBOX_DOMAIN}
TRAEFIK_DASHBOARD_AUTH=${TRAEFIK_DASHBOARD_AUTH}
TRAEFIK_DYNAMIC_CONF_DIR=${HOMEBOX_TRAEFIK_DIR}
EOF

    info "Environment file written to $HOMEBOX_INFRA_DIR/.env"
}

# ─────────────────────────────────────────────────────────────────────────────
# Step 4: Cloudflare Tunnel
# ─────────────────────────────────────────────────────────────────────────────
install_cloudflared() {
    if command -v cloudflared >/dev/null 2>&1; then
        info "cloudflared already installed: $(cloudflared --version 2>&1 | head -1)"
        return 0
    fi

    info "Installing cloudflared..."
    case "$PLATFORM" in
        linux)
            local url="https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-${ARCH}"
            curl -fsSL "$url" -o /usr/local/bin/cloudflared
            chmod +x /usr/local/bin/cloudflared
            ;;
        macos)
            if command -v brew >/dev/null 2>&1; then
                brew install cloudflared
            else
                fail "Install Homebrew (https://brew.sh) first, or install cloudflared manually."
            fi
            ;;
    esac

    info "cloudflared installed: $(cloudflared --version 2>&1 | head -1)"
}

configure_cloudflared() {
    step "Step 4/6 — Cloudflare Tunnel"

    local cred_dir="$HOME/.cloudflared"
    if [ "$HOMEBOX_INSTALL_MODE" != "reinstall" ] && [ -f "$cred_dir/config.yml" ]; then
        info "Keeping existing Cloudflare Tunnel config: $cred_dir/config.yml"
        return 0
    fi

    if ! prompt_yn "Set up a Cloudflare Tunnel?"; then
        info "Skipping Cloudflare Tunnel setup."
        return 0
    fi

    install_cloudflared

    # Authenticate
    info "Authenticating with Cloudflare..."
    info "This will open a browser window. Log in and authorize the tunnel."
    echo ""

    if has_tty; then
        run_with_tty cloudflared tunnel login
    else
        # Headless — try API token
        if [ -n "${CLOUDFLARE_API_TOKEN:-}" ]; then
            info "Using CLOUDFLARE_API_TOKEN for authentication."
        else
            warn "Non-interactive session detected and no CLOUDFLARE_API_TOKEN set."
            warn "To authenticate headless:"
            warn "  1. Create an API token at https://dash.cloudflare.com/profile/api-tokens"
            warn "  2. Export CLOUDFLARE_API_TOKEN=<token>"
            warn "  3. Re-run: bash $SCRIPT_DIR/configure.sh"
            warn ""
            warn "Skipping tunnel setup for now."
            return 0
        fi
    fi

    # Create tunnel
    local tunnel_name="homebox"

    # Check if tunnel already exists
    if cloudflared tunnel list 2>/dev/null | grep -q "$tunnel_name"; then
        info "Tunnel '$tunnel_name' already exists."
        local tunnel_id
        tunnel_id="$(cloudflared tunnel list 2>/dev/null | grep "$tunnel_name" | awk '{print $1}')"
    else
        info "Creating tunnel: $tunnel_name"
        local create_output
        create_output="$(cloudflared tunnel create "$tunnel_name" 2>&1)"
        local tunnel_id
        tunnel_id="$(echo "$create_output" | grep -oE '[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}' | head -1)"
    fi

    if [ -z "${tunnel_id:-}" ]; then
        warn "Could not determine tunnel ID. You may need to configure the tunnel manually."
        return 0
    fi

    info "Tunnel ID: $tunnel_id"

    # Generate config.yml
    mkdir -p "$cred_dir"

    cat > "$cred_dir/config.yml" <<EOF
tunnel: ${tunnel_id}
credentials-file: ${cred_dir}/${tunnel_id}.json

ingress:
  - hostname: "*.${HOMEBOX_DOMAIN}"
    service: http://localhost:80
  - service: http_status:404
EOF

    info "Tunnel config written to $cred_dir/config.yml"

    # DNS routing
    info "Adding DNS route: *.${HOMEBOX_DOMAIN} → tunnel"
    if cloudflared tunnel route dns "$tunnel_name" "*.${HOMEBOX_DOMAIN}" 2>/dev/null; then
        info "DNS route added."
    else
        warn "DNS routing failed. You may need to add a CNAME record manually:"
        warn "  *.${HOMEBOX_DOMAIN} → ${tunnel_id}.cfargotunnel.com"
    fi

    # Install as service
    if [ "$PLATFORM" = "linux" ]; then
        if systemctl is-active --quiet cloudflared 2>/dev/null; then
            info "cloudflared service is already running."
        else
            cloudflared service install 2>/dev/null || true
            systemctl enable --now cloudflared 2>/dev/null || true
            info "cloudflared installed and started as system service."
        fi
    elif [ "$PLATFORM" = "macos" ]; then
        cloudflared service install 2>/dev/null || true
        info "cloudflared installed as launch daemon."
    fi

    success "Cloudflare Tunnel configured."
}

# ─────────────────────────────────────────────────────────────────────────────
# Step 5: GitHub Actions Runner
# ─────────────────────────────────────────────────────────────────────────────
print_manual_runner_instructions() {
    echo ""
    info "To set up the runner manually:"
    info "  1. Go to your repo → Settings → Actions → Runners → New self-hosted runner"
    info "  2. Follow the provided download and configuration commands"
    info "  3. Install as a service: sudo ./svc.sh install && sudo ./svc.sh start"
    echo ""
}

configure_github_runner() {
    step "Step 5/6 — GitHub Actions Self-Hosted Runner"

    local runner_dir="${HOMEBOX_BASE_DIR}/actions-runner"
    if [ "$HOMEBOX_INSTALL_MODE" != "reinstall" ] && [ -f "$runner_dir/.runner" ]; then
        info "Keeping existing GitHub Actions runner: $runner_dir"
        return 0
    fi

    if ! prompt_yn "Set up a GitHub Actions self-hosted runner?"; then
        info "Skipping runner setup."
        return 0
    fi

    # Check for gh CLI
    if ! command -v gh >/dev/null 2>&1; then
        warn "GitHub CLI (gh) not found."

        if prompt_yn "Install GitHub CLI?"; then
            case "$PLATFORM" in
                linux)
                    curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
                        | dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg 2>/dev/null
                    echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
                        | tee /etc/apt/sources.list.d/github-cli.list >/dev/null
                    apt-get update -qq && apt-get install -y -qq gh
                    ;;
                macos)
                    if command -v brew >/dev/null 2>&1; then
                        brew install gh
                    else
                        warn "Install Homebrew first, or install gh manually from https://cli.github.com"
                        print_manual_runner_instructions
                        return 0
                    fi
                    ;;
            esac
        else
            print_manual_runner_instructions
            return 0
        fi
    fi

    # Check gh authentication
    if ! gh auth status >/dev/null 2>&1; then
        info "GitHub CLI is not authenticated. Logging in..."
        gh auth login
    fi

    local repo_url
    repo_url="$(prompt_value "GitHub repository URL (e.g. https://github.com/owner/repo)" "")"

    if [ -z "$repo_url" ]; then
        warn "No repository URL provided. Skipping runner setup."
        return 0
    fi

    local owner_repo token token_error
    case "$repo_url" in
        git@github.com:*)
            owner_repo="${repo_url#git@github.com:}"
            owner_repo="${owner_repo%.git}"
            owner_repo="${owner_repo%/}"
            repo_url="https://github.com/${owner_repo}"
            ;;
        https://github.com/*|http://github.com/*)
            owner_repo="$(echo "$repo_url" | sed 's|^https\{0,1\}://github.com/||' | sed 's|\.git$||' | sed 's|/$||')"
            repo_url="https://github.com/${owner_repo}"
            ;;
        *)
            warn "Unsupported repository URL: $repo_url"
            warn "Use a GitHub repository URL like https://github.com/owner/repo"
            return 0
            ;;
    esac

    info "Using repository: $repo_url"

    # Get registration token
    info "Requesting runner registration token..."
    if token="$(gh api --method POST "repos/${owner_repo}/actions/runners/registration-token" -q '.token' 2>/tmp/homebox-gh-runner.err)"; then
        :
    else
        token=""
        token_error="$(cat /tmp/homebox-gh-runner.err 2>/dev/null)"
    fi

    if [ -z "$token" ]; then
        warn "Could not get registration token."
        if [ -n "${GH_TOKEN:-}" ] || [ -n "${GITHUB_TOKEN:-}" ]; then
            warn "GH_TOKEN or GITHUB_TOKEN is set; gh will prefer that over your saved gh login."
        fi
        warn "Ensure the authenticated account has admin access to ${owner_repo}."
        if [ -n "${token_error:-}" ]; then
            warn "$token_error"
        fi
        print_manual_runner_instructions
        return 0
    fi

    # Determine runner platform string
    local runner_os
    case "$PLATFORM" in
        linux) runner_os="linux" ;;
        macos) runner_os="osx" ;;
    esac

    # Get latest runner version
    info "Fetching latest runner version..."
    local runner_version
    runner_version="$(gh api repos/actions/runner/releases/latest -q '.tag_name' 2>/dev/null | sed 's/^v//')" || true

    if [ -z "$runner_version" ]; then
        warn "Could not determine latest runner version."
        print_manual_runner_instructions
        return 0
    fi

    # Download and extract
    if [ "$HOMEBOX_INSTALL_MODE" = "reinstall" ] && [ -d "$runner_dir" ]; then
        info "Reinstall requested. Removing existing runner files from $runner_dir"
        if [ -x "$runner_dir/svc.sh" ]; then
            (cd "$runner_dir" && ./svc.sh stop 2>/dev/null || true)
            (cd "$runner_dir" && ./svc.sh uninstall 2>/dev/null || true)
        fi
        rm -rf "$runner_dir"
    fi

    mkdir -p "$runner_dir"

    local runner_url="https://github.com/actions/runner/releases/download/v${runner_version}/actions-runner-${runner_os}-${RUNNER_ARCH}-${runner_version}.tar.gz"
    info "Downloading runner v${runner_version} (${runner_os}-${RUNNER_ARCH})..."
    curl -fsSL "$runner_url" | tar xz -C "$runner_dir"

    # Configure
    local runner_name
    runner_name="homebox-$(hostname -s 2>/dev/null || hostname)"

    info "Configuring runner as '$runner_name'..."
    (cd "$runner_dir" && ./config.sh \
        --url "$repo_url" \
        --token "$token" \
        --unattended \
        --name "$runner_name" \
        --labels "homebox,self-hosted" \
        --replace)

    # Install as service
    if [ "$PLATFORM" = "linux" ]; then
        (cd "$runner_dir" && sudo ./svc.sh install && sudo ./svc.sh start)
    elif [ "$PLATFORM" = "macos" ]; then
        (cd "$runner_dir" && ./svc.sh install && ./svc.sh start)
    fi

    success "GitHub Actions runner '$runner_name' installed and started."
}

# ─────────────────────────────────────────────────────────────────────────────
# Step 6: Start infrastructure
# ─────────────────────────────────────────────────────────────────────────────
start_infrastructure() {
    step "Step 6/6 — Starting Base Infrastructure"

    (cd "$HOMEBOX_INFRA_DIR" && docker compose --env-file .env up -d)

    echo ""
    success "Traefik is running."
    info "Dashboard: http://dashboard.${HOMEBOX_DOMAIN}"
}

# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────
main() {
    banner
    info "Interactive configuration for Homebox host"
    info "Base directory: $HOMEBOX_BASE_DIR"
    echo ""

    configure_domain
    configure_dashboard_auth
    generate_env
    configure_cloudflared
    configure_github_runner
    start_infrastructure

    echo ""
    echo "=============================================="
    success "Homebox setup complete!"
    echo "=============================================="
    echo ""
    info "Your host is ready to accept project deployments."
    info "Install the developer CLI on your workstation:"
    info "  pip install ./homebox-infra/cli"
    info "  homebox init"
    echo ""
}

main "$@"
