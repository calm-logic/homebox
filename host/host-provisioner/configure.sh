#!/usr/bin/env bash
# =============================================================================
# Homebox Interactive Configuration
# =============================================================================
# Brings up the Homebox admin UI bound to 127.0.0.1 only. All Cloudflare setup
# (token, tunnel, public URL) happens in the admin's first-run onboarding
# wizard — this script never touches `cloudflared` or the user's personal
# Cloudflare account, so a host that already runs unrelated cloudflared
# tunnels is safe to install on.
#
# Steps:
#   1. Admin credentials (~/.homebox/secrets.json — bcrypt hash only)
#   2. Generate admin .env, copy source into /opt/homebox/admin
#   3. Bring up Traefik (base infrastructure) and the admin stack
#   4. Print local URL + first-run password (user finishes setup in the UI)
# =============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/lib.sh"

require_docker

ADMIN_SRC_DIR="${SCRIPT_DIR}/../admin"
ADMIN_DEPLOY_DIR="${HOMEBOX_BASE_DIR}/admin"
ADMIN_PORT="${HOMEBOX_ADMIN_PORT:-7765}"

HOMEBOX_FIRST_RUN_PASSWORD=""
RESET_PASSWORD=0

for arg in "$@"; do
    case "$arg" in
        --reset-password) RESET_PASSWORD=1 ;;
        --help|-h)
            echo "Usage: configure.sh [--reset-password]"
            echo "  --reset-password   Generate a new admin password (invalidates the old one)"
            exit 0
            ;;
    esac
done

# ─────────────────────────────────────────────────────────────────────────────
# Step 1: Admin credentials
# ─────────────────────────────────────────────────────────────────────────────
load_or_generate_admin_credentials() {
    step "Step 1/4 — Admin credentials"

    ensure_secrets_dir
    HOMEBOX_ADMIN_USERNAME="$(read_secret '.admin.username')"
    HOMEBOX_ADMIN_PASSWORD_HASH="$(read_secret '.admin.password_hash')"

    if [ -z "$HOMEBOX_ADMIN_USERNAME" ]; then
        HOMEBOX_ADMIN_USERNAME="homebox"
    fi

    if [ "$RESET_PASSWORD" -eq 1 ] && [ -n "$HOMEBOX_ADMIN_PASSWORD_HASH" ]; then
        warn "Resetting admin password — the old password is being invalidated."
        HOMEBOX_ADMIN_PASSWORD_HASH=""
    fi

    if [ -z "$HOMEBOX_ADMIN_PASSWORD_HASH" ]; then
        local plain hash
        plain="$(generate_random_password)"
        info "Generating bcrypt hash..."
        hash="$(docker run --rm httpd:2-alpine htpasswd -nbB \
            "$HOMEBOX_ADMIN_USERNAME" "$plain" 2>/dev/null \
            | sed -n "s/^${HOMEBOX_ADMIN_USERNAME}://p")"
        if [ -z "$hash" ]; then
            fail "Failed to generate bcrypt hash."
        fi
        HOMEBOX_ADMIN_PASSWORD_HASH="$hash"
        HOMEBOX_FIRST_RUN_PASSWORD="$plain"
        info "Generated admin credentials (hash saved to $(homebox_secrets_file))"
    else
        info "Reusing existing admin credentials from $(homebox_secrets_file)"
    fi

    prompt_whitelist_email
    write_admin_secrets
}

# Optionally collect a whitelisted email so its owner can sign into Homebox
# passwordlessly via Google/GitHub OAuth (matched against an Identity row). The
# admin app seeds it from secrets.json on startup. Optional + skippable.
prompt_whitelist_email() {
    # Preserve any emails already on disk (requires jq; best-effort otherwise).
    WHITELIST_EMAILS=()
    local e
    while IFS= read -r e; do
        [ -n "$e" ] && WHITELIST_EMAILS+=("$e")
    done < <(read_identities)

    has_tty || return 0  # non-interactive install: skip the prompt entirely

    local answer
    answer="$(prompt_value "Add a whitelisted email for passwordless access? (leave blank to skip)" "")"
    answer="$(echo "$answer" | tr '[:upper:]' '[:lower:]' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')"
    [ -z "$answer" ] && return 0

    if ! [[ "$answer" =~ ^[^@[:space:]]+@[^@[:space:]]+\.[^@[:space:]]+$ ]]; then
        warn "'$answer' doesn't look like an email — skipping."
        return 0
    fi

    # Dedupe.
    for e in "${WHITELIST_EMAILS[@]:-}"; do
        if [ "$e" = "$answer" ]; then
            info "$answer is already whitelisted."
            return 0
        fi
    done
    WHITELIST_EMAILS+=("$answer")
    info "Whitelisted $answer for passwordless login."
}

# Write secrets.json from the current admin + whitelist state. Called once after
# Step 1 settles, so a reused-password install still persists a newly added email.
write_admin_secrets() {
    local ids_json="" first=1 e
    for e in "${WHITELIST_EMAILS[@]:-}"; do
        [ -z "$e" ] && continue
        if [ "$first" -eq 1 ]; then first=0; else ids_json+=", "; fi
        ids_json+="\"${e}\""
    done

    write_secrets_json <<EOF
{
  "admin": {
    "username": "${HOMEBOX_ADMIN_USERNAME}",
    "password_hash": "${HOMEBOX_ADMIN_PASSWORD_HASH}"
  },
  "identities": [${ids_json}]
}
EOF
}

# ─────────────────────────────────────────────────────────────────────────────
# Step 2: Admin .env + source deploy
# ─────────────────────────────────────────────────────────────────────────────
generate_admin_env() {
    step "Step 2/4 — Admin app environment"

    mkdir -p "$ADMIN_DEPLOY_DIR"

    local env_file="$ADMIN_DEPLOY_DIR/.env"
    local db_password app_secret encryption_key dashboard_auth secrets_dir

    if [ -f "$env_file" ] && grep -q '^DB_PASSWORD=' "$env_file"; then
        info "Reusing existing per-deploy secrets from $env_file"
        db_password="$(sed -n 's/^DB_PASSWORD=//p' "$env_file" | head -1)"
        app_secret="$(sed -n 's/^APP_SECRET=//p' "$env_file" | head -1)"
        encryption_key="$(sed -n 's/^ENCRYPTION_KEY=//p' "$env_file" | head -1)"
    else
        info "Generating new per-deploy secrets"
        db_password="$(generate_random_password)"
        app_secret="$(generate_random_hex 32)"
        encryption_key="$(generate_random_hex 32)"
    fi

    # The bcrypt hash from secrets.json is also a valid htpasswd line; escape
    # $ for compose .env consumption.
    dashboard_auth="$(echo "${HOMEBOX_ADMIN_USERNAME}:${HOMEBOX_ADMIN_PASSWORD_HASH}" \
        | sed 's/\$/\$\$/g')"

    secrets_dir="$(homebox_secrets_dir)"

    # No ADMIN_DOMAIN / HOMEBOX_DOMAIN baked in — those come from the admin UI's
    # onboarding flow once the user picks them.
    cat > "$env_file" <<EOF
# Homebox admin — generated $(date -u +%Y-%m-%dT%H:%M:%SZ). Do not commit.
HOMEBOX_ADMIN_PORT=${ADMIN_PORT}
HOMEBOX_ADMIN_USERNAME=${HOMEBOX_ADMIN_USERNAME}
HOMEBOX_SECRETS_DIR=${secrets_dir}
HOMEBOX_HOST_BASE_DIR=${HOMEBOX_BASE_DIR}
DB_PASSWORD=${db_password}
APP_SECRET=${app_secret}
ENCRYPTION_KEY=${encryption_key}
DASHBOARD_AUTH=${dashboard_auth}
EOF
    chmod 600 "$env_file"
    info "Wrote $env_file (mode 600)"
}

deploy_admin_source() {
    step "Step 3/4 — Deploying admin source"

    if [ ! -d "$ADMIN_SRC_DIR" ]; then
        fail "Admin source not found at $ADMIN_SRC_DIR"
    fi

    if command -v rsync >/dev/null 2>&1; then
        rsync -a --delete \
            --exclude '.env' \
            --exclude '__pycache__' \
            --exclude '.venv' \
            "$ADMIN_SRC_DIR/" "$ADMIN_DEPLOY_DIR/"
    else
        local tmp_env=""
        if [ -f "$ADMIN_DEPLOY_DIR/.env" ]; then
            tmp_env="$(mktemp)"
            cp "$ADMIN_DEPLOY_DIR/.env" "$tmp_env"
        fi
        rm -rf "$ADMIN_DEPLOY_DIR"
        mkdir -p "$ADMIN_DEPLOY_DIR"
        cp -R "$ADMIN_SRC_DIR"/. "$ADMIN_DEPLOY_DIR/"
        if [ -n "$tmp_env" ]; then
            cp "$tmp_env" "$ADMIN_DEPLOY_DIR/.env"
            rm -f "$tmp_env"
        fi
    fi
    info "Admin source deployed to $ADMIN_DEPLOY_DIR"
}

# ─────────────────────────────────────────────────────────────────────────────
# Step 4: Bring up Traefik + admin
# ─────────────────────────────────────────────────────────────────────────────
start_stacks() {
    step "Step 4/4 — Starting Traefik + admin"

    if ! docker network inspect traefik-net >/dev/null 2>&1; then
        docker network create traefik-net
    fi

    # Refresh base-infrastructure files from source (the .env is preserved).
    local base_src="${SCRIPT_DIR}/base-infrastructure"
    if [ -d "$base_src" ]; then
        mkdir -p "$HOMEBOX_INFRA_DIR" "$HOMEBOX_TRAEFIK_DIR"
        cp "$base_src/docker-compose.yml" "$HOMEBOX_INFRA_DIR/docker-compose.yml"
        if [ -f "$base_src/.env.example" ]; then
            cp "$base_src/.env.example" "$HOMEBOX_INFRA_DIR/.env.example"
        fi
        info "Refreshed base infrastructure from source."
    fi

    # Base infrastructure (Traefik). Cloudflared is *not* started here — the
    # admin app launches it after the user completes onboarding.
    if [ -f "$HOMEBOX_INFRA_DIR/docker-compose.yml" ]; then
        local base_env="$HOMEBOX_INFRA_DIR/.env"
        local dashboard_auth_for_base
        dashboard_auth_for_base="$(echo "${HOMEBOX_ADMIN_USERNAME}:${HOMEBOX_ADMIN_PASSWORD_HASH}" \
            | sed 's/\$/\$\$/g')"
        cat > "$base_env" <<EOF
TRAEFIK_DASHBOARD_AUTH=${dashboard_auth_for_base}
TRAEFIK_DYNAMIC_CONF_DIR=${HOMEBOX_TRAEFIK_DIR}
EOF
        chmod 600 "$base_env"

        # Empty dynamic config so Traefik starts cleanly. The admin app
        # rewrites this with the admin route after onboarding step 3.
        local dyn="$HOMEBOX_TRAEFIK_DIR/dynamic_conf.yml"
        if [ ! -f "$dyn" ]; then
            cat > "$dyn" <<'EOF'
# Managed by Homebox admin — onboarding wizard rewrites this once the
# admin's public hostname is chosen.
http:
  routers: {}
  services: {}
EOF
            chmod 644 "$dyn"
        fi

        info "Bringing up Traefik (cloudflared will be started by the admin after onboarding)."
        (cd "$HOMEBOX_INFRA_DIR" && docker compose --env-file .env up -d)
    else
        warn "Base infrastructure not found at $HOMEBOX_INFRA_DIR — skipping Traefik."
    fi

    (cd "$ADMIN_DEPLOY_DIR" && docker compose --env-file .env up -d --build)
    success "Admin stack is up."
}

# ─────────────────────────────────────────────────────────────────────────────
# Summary
# ─────────────────────────────────────────────────────────────────────────────
print_summary_and_open() {
    local local_url="http://localhost:${ADMIN_PORT}"

    echo ""
    echo "=============================================="
    success "Homebox admin is ready"
    echo "=============================================="
    info "Local URL  : $local_url"
    info "Username   : $HOMEBOX_ADMIN_USERNAME"
    if [ -n "$HOMEBOX_FIRST_RUN_PASSWORD" ]; then
        echo ""
        printf "${BOLD}${GREEN}First-run password (write this down — it will not be shown again):${NC}\n"
        printf "${BOLD}    %s${NC}\n" "$HOMEBOX_FIRST_RUN_PASSWORD"
        echo ""
        info "Only the bcrypt hash is persisted in $(homebox_secrets_file)."
        info "Lost it? Re-run 'make configure --reset-password' to regenerate."
    else
        info "Password   : reused (hash on disk in $(homebox_secrets_file))"
    fi
    echo ""
    info "The admin is bound to 127.0.0.1 only. Reach it from this host directly,"
    info "or via SSH tunnel from another machine:"
    info "    ssh -L ${ADMIN_PORT}:localhost:${ADMIN_PORT} <this-host>"
    echo ""
    info "First login starts the onboarding wizard — it'll walk you through"
    info "connecting Cloudflare and assigning a public URL for the admin."
    echo ""

    case "$PLATFORM" in
        macos) open "$local_url" >/dev/null 2>&1 || true ;;
        linux)
            if command -v xdg-open >/dev/null 2>&1; then
                if [ -n "${SUDO_USER:-}" ]; then
                    sudo -u "$SUDO_USER" xdg-open "$local_url" >/dev/null 2>&1 || true
                else
                    xdg-open "$local_url" >/dev/null 2>&1 || true
                fi
            fi
            ;;
    esac
}

# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────
main() {
    banner
    info "Homebox configure — admin bootstrap (localhost-only; Cloudflare set up in the UI)"
    info "Base directory: $HOMEBOX_BASE_DIR"
    echo ""

    load_or_generate_admin_credentials
    generate_admin_env
    deploy_admin_source
    start_stacks
    install_boot_unit
    print_summary_and_open
}

main "$@"
