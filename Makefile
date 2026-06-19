SHELL := /usr/bin/env bash

PROVISIONER_DIR := homebox-infra/host-provisioner
SETUP_HOST     := $(PROVISIONER_DIR)/setup_host.sh
CONFIGURE_HOST := $(PROVISIONER_DIR)/configure.sh

UNAME_S := $(shell uname -s)
ifeq ($(UNAME_S),Linux)
SUDO := sudo
else
SUDO :=
endif

.PHONY: help host configure reset-password admin admin-logs admin-down cli init

help:
	@echo "Homebox targets:"
	@echo "  make host        Provision this machine (Docker, dirs, network) and bring up the admin UI"
	@echo "  make configure       Re-run the admin bootstrap (regenerates .env, redeploys app)"
	@echo "  make reset-password  Generate a new admin password (invalidates the old one)"
	@echo "  make admin       Rebuild & restart the admin app stack only (preserves DB)"
	@echo "  make admin-logs  Tail admin app logs"
	@echo "  make admin-down  Stop the admin app stack"
	@echo "  make cli         Install the developer CLI from ./homebox-infra/cli"
	@echo "  make init        Initialize the developer CLI (~/.homebox.json)"

host:
	$(SUDO) bash $(SETUP_HOST)

configure:
	$(SUDO) bash $(CONFIGURE_HOST)

reset-password:
	$(SUDO) bash $(CONFIGURE_HOST) --reset-password

admin:
	@echo ">>> Syncing admin source from repo to /opt/homebox/admin"
	$(SUDO) rsync -a --delete --exclude '.env' --exclude 'node_modules' --exclude 'dist' --exclude '__pycache__' --exclude '.venv' homebox-infra/admin/ /opt/homebox/admin/
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

cli:
	pip install ./homebox-infra/cli

init:
	homebox init
