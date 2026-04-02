# Homebox

A self-hosted Internal PaaS for deploying and managing containerized applications on a single server. Homebox uses Traefik as a reverse proxy, Cloudflare Tunnel for internet exposure, and provides a CLI for developers to switch between local development and published container routing.

## Architecture

```
Internet
  │
  ▼
Cloudflare Tunnel
  │
  ▼
Traefik (port 80)
  ├── app1.example.com → app1 container  (pub mode)
  ├── app2.example.com → dev machine IP  (dev mode)
  └── app3.example.com → app3 container  (pub mode)
```

Each project runs in its own Docker Compose stack with isolated networking:

- **traefik-net** (shared) — connects project containers to Traefik for HTTP routing
- **\<project\>-internal** (per-project) — connects app, database, and cache containers privately

Backing services (Postgres, Redis) are never exposed to the host network.

## Project Structure

```
homebox-infra/
├── cli/                        # Python CLI for developers
│   ├── homebox_cli/
│   │   ├── main.py             # Commands: init, switch, db sync
│   │   ├── config.py           # ~/.homebox.json management
│   │   ├── network.py          # Local IP detection
│   │   ├── ssh.py              # SSH client for remote operations
│   │   └── traefik.py          # Traefik config file manipulation
│   └── pyproject.toml
├── host-provisioner/
│   ├── base-infrastructure/
│   │   ├── docker-compose.yml  # Traefik service definition
│   │   ├── .env.example        # Host configuration template
│   │   └── dynamic_conf.yml    # Traefik dynamic routing rules
│   └── setup_host.sh           # Host provisioning script
└── docs/
    └── claude-bootstrap-skill.md  # Guide for scaffolding new projects
```

## Host Setup

### Prerequisites

- A Linux server (Ubuntu/Debian)
- Root access
- A domain managed by Cloudflare

### Provision the host

```bash
sudo ./homebox-infra/host-provisioner/setup_host.sh
```

This installs Docker, creates the directory structure at `/opt/homebox`, deploys base infrastructure files, and creates the shared `traefik-net` Docker network.

### Configure and start

1. Copy and edit the environment file:
   ```bash
   cp /opt/homebox/base-infrastructure/.env.example /opt/homebox/base-infrastructure/.env
   ```

2. Set your values:
   ```env
   HOMEBOX_DOMAIN=example.com
   TRAEFIK_DASHBOARD_AUTH=admin:$apr1$...    # generate with: htpasswd -n admin
   TRAEFIK_DYNAMIC_CONF_DIR=/opt/homebox/traefik
   ```

3. Set up a Cloudflare Tunnel pointing to `localhost:80`.

4. Start Traefik:
   ```bash
   cd /opt/homebox/base-infrastructure
   docker compose --env-file .env up -d
   ```

## Developer Setup

### Install the CLI

```bash
pip install ./homebox-infra/cli
```

### Initialize

```bash
homebox init
```

This prompts for:
- Host server LAN IP
- SSH username and key path
- Cloudflare domain
- Traefik config path (default: `/opt/homebox/traefik/dynamic_conf.yml`)
- Projects directory (default: `/opt/homebox/projects`)

Configuration is saved to `~/.homebox.json`.

## CLI Usage

### Switch routing modes

Route a project subdomain to your local development server:

```bash
homebox switch myapp dev --port 8000
```

Route back to the published container on the host:

```bash
homebox switch myapp pub
```

The CLI connects to the host via SSH and updates Traefik's dynamic configuration file. Changes take effect immediately.

### Sync a database locally

Pull a project's database from the host to your local machine:

```bash
homebox db sync myapp
```

With explicit options:

```bash
homebox db sync myapp --db myapp_db --user myapp_user --local-db myapp_dev --container myapp-db-1
```

## Deploying a New Project

Each project needs:

1. A **Dockerfile** with layer caching and a non-root user
2. A **docker-compose.yml** with Traefik labels and internal networking
3. A **GitHub Actions workflow** for CI/CD (using a self-hosted runner on the host)

Traefik labels for production routing:

```yaml
labels:
  - "traefik.enable=true"
  - "traefik.http.routers.myapp.rule=Host(`myapp.example.com`)"
  - "traefik.http.routers.myapp.entrypoints=web"
  - "traefik.http.services.myapp.loadbalancer.server.port=8000"
  - "traefik.docker.network=traefik-net"
```

See [`homebox-infra/docs/claude-bootstrap-skill.md`](homebox-infra/docs/claude-bootstrap-skill.md) for a detailed project scaffolding guide.

## Development Workflow

1. **Start developing** — run your app locally and switch routing to dev mode:
   ```bash
   homebox switch myapp dev --port 8000
   ```
   Your app is now accessible at `myapp.example.com`, routed to your machine.

2. **Sync production data** (optional):
   ```bash
   homebox db sync myapp
   ```

3. **Deploy** — push to your branch and let GitHub Actions build and deploy the container. Then switch back:
   ```bash
   homebox switch myapp pub
   ```

## Tech Stack

| Component | Technology |
|-----------|-----------|
| Reverse proxy | Traefik v3 |
| Tunneling | Cloudflare Tunnel |
| Containers | Docker & Docker Compose |
| Database | PostgreSQL 16 |
| Cache | Valkey (Redis-compatible) 7 |
| CLI | Python 3.10+, Typer, Paramiko |
| CI/CD | GitHub Actions (self-hosted runner) |
