SHELL := /usr/bin/env bash

PROVISIONER_DIR := host/host-provisioner
SETUP_HOST     := $(PROVISIONER_DIR)/setup_host.sh
CONFIGURE_HOST := $(PROVISIONER_DIR)/configure.sh

UNAME_S := $(shell uname -s)
ifeq ($(UNAME_S),Linux)
SUDO := sudo
else
SUDO :=
endif

.PHONY: help host configure reset-password admin admin-logs admin-down admin-reset infra reonboard boot enable-boot cli init push-public-github deploy-site

DB_CONTAINER := homebox-admin-db

help:
	@echo "Homebox targets:"
	@echo "  make host        Provision this machine (Docker, dirs, network) and bring up the admin UI"
	@echo "  make configure       Re-run the admin bootstrap (regenerates .env, redeploys app)"
	@echo "  make reset-password  Generate a new admin password (invalidates the old one)"
	@echo "  make admin       Rebuild & restart the admin app stack only (preserves DB)"
	@echo "  make admin-logs  Tail admin app logs"
	@echo "  make admin-down  Stop the admin app stack"
	@echo "  make admin-reset WIPES the admin DB (down -v) then rebuilds — you re-onboard after"
	@echo "  make infra       Sync & re-up the base-infrastructure stack (Traefik + docker-proxy)"
	@echo "  make reonboard   Clear the Cloudflare connection so onboarding runs again (keeps projects/identities)"
	@echo "  make boot        Bring the whole stack up now (same script systemd runs on boot)"
	@echo "  make enable-boot Install + enable the systemd unit so Homebox auto-starts on boot"
	@echo "  make cli         Install the developer CLI from ./host/cli"
	@echo "  make init        Initialize the developer CLI (~/.homebox.json)"
	@echo "  make push-public-github  Dry-run the public-mirror publish; PUSH=1 pushes for real (private repo only)"
	@echo "  make deploy-site Build & deploy the homebox.sh site (private repo only)"

host:
	$(SUDO) bash $(SETUP_HOST)

configure:
	$(SUDO) bash $(CONFIGURE_HOST)

reset-password:
	$(SUDO) bash $(CONFIGURE_HOST) --reset-password

admin:
	@echo ">>> Syncing admin source from repo to /opt/homebox/admin"
	$(SUDO) rsync -a --delete --exclude '.env' --exclude 'cluster-keys.json' --exclude 'node_modules' --exclude 'dist' --exclude '__pycache__' --exclude '.venv' host/admin/ /opt/homebox/admin/
	$(SUDO) bash -c 'cd /opt/homebox/admin && docker compose --env-file .env up -d --build'
	@port=$$($(SUDO) sed -n 's/^HOMEBOX_ADMIN_PORT=//p' /opt/homebox/admin/.env 2>/dev/null | head -1); \
	  port=$${port:-7765}; \
	  bind=$$($(SUDO) sed -n 's/^HOMEBOX_ADMIN_BIND=//p' /opt/homebox/admin/.env 2>/dev/null | head -1); \
	  bind=$${bind:-127.0.0.1}; \
	  echo ""; \
	  echo ">>> Admin is up at http://$$bind:$$port"; \
	  if [ "$$bind" = "127.0.0.1" ]; then \
	    echo "    (localhost-only — from another machine: ssh -L $$port:localhost:$$port <this-host>)"; \
	  fi

admin-logs:
	$(SUDO) bash -c 'cd /opt/homebox/admin && docker compose logs -f --tail=200'

admin-down:
	$(SUDO) bash -c 'cd /opt/homebox/admin && docker compose down'

admin-reset:
	@echo ">>> WIPING the admin database volume and rebuilding (you will re-onboard)."
	$(SUDO) rsync -a --delete --exclude '.env' --exclude 'cluster-keys.json' --exclude 'node_modules' --exclude 'dist' --exclude '__pycache__' --exclude '.venv' host/admin/ /opt/homebox/admin/
	$(SUDO) bash -c 'cd /opt/homebox/admin && docker compose --env-file .env down -v && docker compose --env-file .env up -d --build'
	@echo ">>> Admin reset complete. Sign in and re-run onboarding."

infra:
	@echo ">>> Syncing base-infrastructure from repo to /opt/homebox/base-infrastructure"
	$(SUDO) rsync -a --delete --exclude '.env' --exclude 'domains.json' host/host-provisioner/base-infrastructure/ /opt/homebox/base-infrastructure/
	$(SUDO) bash -c 'cd /opt/homebox/base-infrastructure && docker compose --env-file .env up -d'
	@echo ""
	@echo ">>> Base infrastructure updated. Traefik image now:"
	@$(SUDO) docker inspect homebox-traefik --format '    {{.Config.Image}}' 2>/dev/null || true

reonboard:
	@echo ">>> Clearing the Cloudflare connection so the onboarding wizard runs again."
	@echo "    (Keeps GitHub integrations, projects, and identities. The Cloudflare-side"
	@echo "     tunnel is left in place and re-adopted on the next onboarding run.)"
	$(SUDO) docker exec $(DB_CONTAINER) psql -U homebox_admin -d homebox_admin -c \
	  "DELETE FROM integrations WHERE provider='cloudflare'; DELETE FROM settings WHERE key='admin_domain';"
	-$(SUDO) docker rm -f homebox-cloudflared
	@echo ">>> Done. Reload the admin UI — onboarding will reappear."

boot:
	$(SUDO) bash $(PROVISIONER_DIR)/homebox-boot.sh

enable-boot:
	$(SUDO) bash -c 'source $(PROVISIONER_DIR)/lib.sh && install_boot_unit'

cli:
	pip install ./host/cli

init:
	homebox init

# --- private-repo-only targets ------------------------------------------------
# This Makefile is itself published to the public mirror (calm-logic/homebox),
# where scripts/ and cloud/ do not exist — so these targets guard on the files
# they need and no-op with a message in the public checkout.

push-public-github:
	@if [ ! -f scripts/publish_public.sh ]; then \
	  echo "push-public-github is a private-repo-only target (scripts/publish_public.sh not present)"; \
	else \
	  if [ "$(PUSH)" = "1" ]; then \
	    bash scripts/publish_public.sh --push; \
	  else \
	    bash scripts/publish_public.sh; \
	  fi; \
	fi

deploy-site:
	@if [ ! -f cloud/scripts/setup_site.sh ]; then \
	  echo "deploy-site is a private-repo-only target (cloud/scripts/setup_site.sh not present)"; \
	else \
	  bash cloud/scripts/setup_site.sh; \
	fi
