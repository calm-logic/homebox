# Homebox Project Bootstrap Skill

> **Purpose:** Give this document to an LLM (e.g. Claude) when you want to scaffold a **new** project or make an **existing** project Homebox-compatible. The LLM will generate or adapt a production Dockerfile, a Host-ready `docker-compose.yml`, and a GitHub Actions CI/CD workflow.

---

## Context you MUST provide to the LLM

Before invoking this skill, tell the LLM:

| Variable | Example | Description |
|---|---|---|
| `PROJECT_NAME` | `myapp` | Lowercase, no spaces. Used for container names, subdomains, and DB names. |
| `LANGUAGE / FRAMEWORK` | `Python / FastAPI` | So the Dockerfile and test commands are correct. |
| `APP_PORT` | `8000` | The port the application listens on inside the container. |
| `NEEDS_POSTGRES` | `yes` | Whether the project uses PostgreSQL. |
| `NEEDS_REDIS` | `yes` / `no` | Whether the project uses Redis / Valkey. |
| `EXTRA_SERVICES` | `none` | Any other backing services (e.g. `rabbitmq`, `meilisearch`). |

---

## Instructions for the LLM

### Pre-Generation Assessment (for existing repos)

Before generating any files, check what already exists in the project:

1. **Does a `Dockerfile` exist?**
   - **YES:** Review it. Ensure it follows the rules below (slim/alpine base, pinned version, non-root user, layer-cached dependencies). If it deviates, suggest **specific, minimal modifications** rather than replacing it wholesale. Preserve the existing build logic, package installations, and any project-specific steps.
   - **NO:** Generate one per Section 1 below.

2. **Does a `docker-compose.yml` exist?**
   - **YES:** Review it. Add the required Traefik labels and `traefik-net` network configuration. Do NOT remove existing services or configuration that does not conflict with Homebox requirements. If the app service already has networks defined, add `traefik-net` to the list. If there are existing labels, append the Traefik labels.
   - **NO:** Generate one per Section 2 below.

3. **Does a `.github/workflows/deploy.yml` (or similar CI file) exist?**
   - **YES:** Check if it already handles deployment. If it deploys elsewhere (e.g., Heroku, AWS, Vercel), offer to add a Homebox deploy job **alongside** the existing one, or replace it if the user confirms.
   - **NO:** Generate one per Section 4 below.

4. **Does a `.env.example` exist?**
   - **YES:** Add `HOMEBOX_DOMAIN` and `DB_PASSWORD` if they are missing. Do NOT remove existing variables.
   - **NO:** Generate one per Section 3 below.

When modifying existing files, present the changes as diffs or clearly marked additions so the user can review what changed.

---

You are generating files for a project that will run on a **Homebox** internal PaaS. The infrastructure uses:

- **Traefik v3** as a reverse proxy on a shared Docker network called `traefik-net`.
- **Cloudflare Tunnel** to expose `*.DOMAIN` to the internet.
- **Per-project Docker Compose stacks** — each project runs its own database, cache, and app containers. There is NO shared database.
- **GitHub Actions with a self-hosted runner** on the Host machine for CI/CD.

Generate exactly the files described below. Do not deviate from the structure.

---

### 1. `Dockerfile`

```dockerfile
# -- Build stage (if compiled language) or single stage --
FROM <appropriate-base>:<version>-slim AS base

WORKDIR /app

# Install dependencies first (layer caching)
COPY <dependency-manifest> .
RUN <install-dependencies>

# Copy application code
COPY . .

# Build step if needed
RUN <build-command-if-needed>

# Run as non-root
RUN adduser --disabled-password --no-create-home appuser
USER appuser

EXPOSE ${APP_PORT}

CMD ["<start-command>"]
```

**Rules:**
- Use a `-slim` or `-alpine` base image.
- Separate dependency installation from code copy for layer caching.
- Always run as a non-root user.
- Use `EXPOSE` matching `APP_PORT`.
- Do NOT use `latest` tags — pin a specific version.

---

### 2. `docker-compose.yml`

This file is what runs on the Host via `docker compose up -d`.

```yaml
services:
  app:
    build: .
    container_name: ${PROJECT_NAME}-app
    restart: unless-stopped
    environment:
      DATABASE_URL: "postgresql://${PROJECT_NAME}:${DB_PASSWORD}@postgres:5432/${PROJECT_NAME}"
      # Add REDIS_URL only if NEEDS_REDIS == yes:
      # REDIS_URL: "redis://redis:6379/0"
    depends_on:
      postgres:
        condition: service_healthy
    networks:
      - default
      - traefik-net
    labels:
      - "traefik.enable=true"
      - "traefik.http.routers.${PROJECT_NAME}.rule=Host(`${PROJECT_NAME}.${HOMEBOX_DOMAIN}`)"
      - "traefik.http.routers.${PROJECT_NAME}.entrypoints=web"
      - "traefik.http.services.${PROJECT_NAME}.loadbalancer.server.port=${APP_PORT}"
      - "traefik.docker.network=traefik-net"

  postgres:
    image: postgres:16-alpine
    container_name: ${PROJECT_NAME}-postgres
    restart: unless-stopped
    environment:
      POSTGRES_USER: ${PROJECT_NAME}
      POSTGRES_PASSWORD: ${DB_PASSWORD}
      POSTGRES_DB: ${PROJECT_NAME}
    volumes:
      - pgdata:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U ${PROJECT_NAME}"]
      interval: 5s
      timeout: 3s
      retries: 5
    networks:
      - default

  # Include ONLY if NEEDS_REDIS == yes:
  # redis:
  #   image: redis:7-alpine
  #   container_name: ${PROJECT_NAME}-redis
  #   restart: unless-stopped
  #   volumes:
  #     - redisdata:/data
  #   networks:
  #     - default

volumes:
  pgdata:
  # redisdata:  # uncomment if redis is included

networks:
  default:
    name: ${PROJECT_NAME}-internal
  traefik-net:
    external: true
```

**Rules:**
- The `app` service MUST join both `default` (internal, for DB/cache access) and `traefik-net` (external, for Traefik routing).
- Backing services (postgres, redis, etc.) MUST only be on the `default` network — never on `traefik-net`, never with `ports:` published to the host.
- Use named volumes for all data persistence.
- Use `container_name: ${PROJECT_NAME}-<service>` so the `homebox db sync` CLI command can find them.
- The `HOMEBOX_DOMAIN` variable is read from the Host's environment or a `.env` file in the project directory.
- Add health checks to database services and use `depends_on` with `condition: service_healthy`.

---

### 3. `.env.example`

```env
HOMEBOX_DOMAIN=example.com
DB_PASSWORD=changeme
# Add any project-specific env vars below
```

---

### 4. `.github/workflows/deploy.yml`

```yaml
name: Deploy to Homebox

on:
  push:
    branches: [main]

jobs:
  test:
    runs-on: ubuntu-latest

    steps:
      - name: Checkout
        uses: actions/checkout@v4

      - name: Run tests
        run: |
          # Run tests in CI (cloud runner)
          # Replace with your project's test command, e.g.:
          #   npm test
          #   pytest
          #   go test ./...
          echo "No tests configured — add your test command here"

  deploy:
    runs-on: self-hosted
    needs: test

    steps:
      - name: Checkout
        uses: actions/checkout@v4

      - name: Deploy
        run: |
          docker compose down --remove-orphans
          docker compose up -d --build

      - name: Verify
        run: |
          sleep 5
          docker compose ps
          # Optional: curl health check
          # curl -f http://localhost:${APP_PORT}/health || exit 1
```

**Rules:**
- Tests run on `ubuntu-latest` (cloud runner) — fast, isolated, no load on your host.
- Deployment runs on `self-hosted` (your Homebox host) — only after tests pass (`needs: test`).
- Replace the placeholder test command with your project's actual test runner.
- Use `docker compose down --remove-orphans` then `up -d --build` for zero-ambiguity deploys.
- The workflow should assume the working directory is the project root, and that a `.env` file already exists on the Host (placed during initial setup).

---

### 5. Project-level `README.md` section to include

Generate a section like this in the project README:

```markdown
## Homebox Deployment

This project deploys on the Homebox internal PaaS.

- **Live URL:** `https://<PROJECT_NAME>.<DOMAIN>`
- **Switch to dev:** `homebox switch <PROJECT_NAME> dev --port <APP_PORT>`
- **Switch to pub:** `homebox switch <PROJECT_NAME> pub`
- **Sync DB locally:** `homebox db sync <PROJECT_NAME>`
```

---

## Checklist for the LLM

Before presenting the generated files, verify:

- [ ] Dockerfile uses a pinned, slim base image and runs as non-root
- [ ] `docker-compose.yml` has `traefik-net` as an `external: true` network
- [ ] The `app` service has correct Traefik labels using `${HOMEBOX_DOMAIN}`
- [ ] Backing services (postgres, redis) have NO `ports:` section
- [ ] Backing services are only on the internal network, not `traefik-net`
- [ ] Container names follow the `${PROJECT_NAME}-<service>` convention
- [ ] `.github/workflows/deploy.yml` uses `runs-on: self-hosted`
- [ ] Tests run before deploy in the workflow
- [ ] All hardcoded values are replaced with the user's provided variables

**Additional checks for existing repos:**

- [ ] Existing files were assessed before generating new ones
- [ ] Existing Dockerfile was adapted (not replaced) if it existed
- [ ] Existing `docker-compose.yml` had Traefik config merged in (not replaced)
- [ ] Existing `.env.example` had Homebox vars added (not replaced)
- [ ] Existing CI workflows were preserved alongside new Homebox deploy job
