# Homebox

A self-hosted Internal PaaS for deploying and managing containerized applications. Homebox runs on any machine with Docker (Linux, macOS, or Windows), uses Traefik as a reverse proxy, Cloudflare Tunnel for internet exposure, and provides a CLI for developers to switch between local development and published container routing.

## Quick Start

Install on any machine with a single command:

**macOS / Linux:**
```bash
curl -fsSL https://raw.githubusercontent.com/aleontiev/homebox/main/homebox-infra/install.sh | bash
```

**Windows (PowerShell as Administrator):**
```powershell
irm https://raw.githubusercontent.com/aleontiev/homebox/main/homebox-infra/install.ps1 | iex
```

The installer will:
1. Check for (or install) Docker
2. Create the Homebox directory structure
3. Walk you through domain, authentication, Cloudflare Tunnel, and GitHub runner setup
4. Start Traefik

## Architecture

```
Internet
  |
  v
Cloudflare Tunnel
  |
  v
Traefik (port 80)
  |-- app1.example.com -> app1 container  (pub mode)
  |-- app2.example.com -> dev machine IP  (dev mode)
  +-- app3.example.com -> app3 container  (pub mode)
```

Each project runs in its own Docker Compose stack with isolated networking:

- **traefik-net** (shared) — connects project containers to Traefik for HTTP routing
- **\<project\>-internal** (per-project) — connects app, database, and cache containers privately

Backing services (Postgres, Redis) are never exposed to the host network.

## Project Structure

```
homebox-infra/
├── install.sh / install.ps1       # One-liner installers
├── cli/                           # Python CLI for developers
│   ├── homebox_cli/
│   │   ├── main.py                # Commands: init, switch, db sync
│   │   ├── config.py              # ~/.homebox.json management
│   │   ├── network.py             # Local IP detection
│   │   ├── ssh.py                 # SSH client for remote operations
│   │   └── traefik.py             # Traefik config file manipulation
│   └── pyproject.toml
├── host-provisioner/
│   ├── base-infrastructure/
│   │   ├── docker-compose.yml     # Traefik service definition
│   │   ├── .env.example           # Host configuration template
│   │   └── dynamic_conf.yml       # Traefik dynamic routing rules
│   ├── lib.sh                     # Shared utilities (colors, platform detection)
│   ├── setup_host.sh              # Host provisioning script
│   └── configure.sh               # Interactive configuration
└── docs/
    ├── homebox-ready.md           # Making a project Homebox-compatible
    └── claude-bootstrap-skill.md  # LLM guide for scaffolding projects
```

## Host Setup

### Prerequisites

- **Any machine** — Linux, macOS (including Mac Mini), or Windows
- **Docker** — Docker Engine on Linux, Docker Desktop on macOS/Windows
- A domain managed by Cloudflare (for tunnel access)

### One-liner install (recommended)

See [Quick Start](#quick-start) above. The installer handles everything interactively.

### Manual setup

If you prefer to run the provisioner directly:

```bash
# Linux (requires sudo)
sudo ./homebox-infra/host-provisioner/setup_host.sh

# macOS (no sudo required)
./homebox-infra/host-provisioner/setup_host.sh
```

The provisioner installs Docker, creates the directory structure, deploys base infrastructure files, creates the `traefik-net` Docker network, and launches the interactive configurator.

**Default paths:**
| Platform | Base directory |
|----------|---------------|
| Linux    | `/opt/homebox` |
| macOS    | `~/homebox` |
| Windows  | `%USERPROFILE%\homebox` |

Override with `HOMEBOX_BASE_DIR` environment variable.

To re-run just the configuration (domain, auth, tunnel, runner):

```bash
bash homebox-infra/host-provisioner/configure.sh
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

## Making a Project Homebox-Ready

Any project with a `docker-compose.yml` that includes Traefik labels and joins the `traefik-net` network is Homebox-compatible. See the full guide: **[homebox-ready.md](homebox-infra/docs/homebox-ready.md)**.

The minimum requirements:

1. An app service with Traefik routing labels
2. The app service joins the external `traefik-net` network
3. Backing services stay on an internal network (no host ports)

To scaffold a new project or add Homebox support to an existing repo, see the [bootstrap skill](homebox-infra/docs/claude-bootstrap-skill.md).

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

3. **Deploy** — push to your branch. GitHub Actions runs tests in the cloud, then deploys to your host:
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
| CI/CD | GitHub Actions (tests in cloud, deploy on self-hosted runner) |
| Host platforms | Linux, macOS, Windows |
