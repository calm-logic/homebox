# Making a Project Homebox-Ready

A project is Homebox-compatible when it can be deployed onto a Homebox host with `docker compose up -d` and automatically receive traffic through Traefik.

## Requirements (V1)

The only requirement is a **`docker-compose.yml`** at your project root with an app service configured for Traefik routing.

### 1. Traefik labels on your app service

Your app service must include these labels so Traefik discovers and routes to it:

```yaml
labels:
  - "traefik.enable=true"
  - "traefik.http.routers.MYAPP.rule=Host(`MYAPP.${HOMEBOX_DOMAIN}`)"
  - "traefik.http.routers.MYAPP.entrypoints=web"
  - "traefik.http.services.MYAPP.loadbalancer.server.port=PORT"
  - "traefik.docker.network=traefik-net"
```

Replace `MYAPP` with your project name and `PORT` with the port your app listens on.

### 2. Join the shared `traefik-net` network

Your app service must connect to the external `traefik-net` network (shared with Traefik). Declare it at the bottom of your compose file:

```yaml
networks:
  traefik-net:
    external: true
```

And attach your app service to it:

```yaml
services:
  app:
    networks:
      - default
      - traefik-net
```

### 3. Keep backing services internal

Database, cache, and other backing services should **only** be on the `default` (internal) network. They must **not** join `traefik-net` and must **not** publish host ports. This prevents port conflicts between projects and keeps services private.

## Minimal Example

An app with no backing services:

```yaml
services:
  app:
    build: .
    restart: unless-stopped
    networks:
      - traefik-net
    labels:
      - "traefik.enable=true"
      - "traefik.http.routers.myapp.rule=Host(`myapp.${HOMEBOX_DOMAIN}`)"
      - "traefik.http.routers.myapp.entrypoints=web"
      - "traefik.http.services.myapp.loadbalancer.server.port=8000"
      - "traefik.docker.network=traefik-net"

networks:
  traefik-net:
    external: true
```

## Full Example

An app with Postgres and Redis:

```yaml
services:
  app:
    build: .
    restart: unless-stopped
    depends_on:
      postgres:
        condition: service_healthy
      redis:
        condition: service_started
    environment:
      DATABASE_URL: postgres://postgres:${DB_PASSWORD}@postgres:5432/myapp
      REDIS_URL: redis://redis:6379/0
    networks:
      - default
      - traefik-net
    labels:
      - "traefik.enable=true"
      - "traefik.http.routers.myapp.rule=Host(`myapp.${HOMEBOX_DOMAIN}`)"
      - "traefik.http.routers.myapp.entrypoints=web"
      - "traefik.http.services.myapp.loadbalancer.server.port=8000"
      - "traefik.docker.network=traefik-net"

  postgres:
    image: postgres:16-alpine
    restart: unless-stopped
    environment:
      POSTGRES_DB: myapp
      POSTGRES_PASSWORD: ${DB_PASSWORD}
    volumes:
      - pgdata:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U postgres"]
      interval: 5s
      timeout: 3s
      retries: 5

  redis:
    image: valkey/valkey:7-alpine
    restart: unless-stopped

volumes:
  pgdata:

networks:
  traefik-net:
    external: true
```

## Checklist

- [ ] `docker-compose.yml` exists at project root
- [ ] App service has all 5 Traefik labels
- [ ] App service joins `traefik-net`
- [ ] `traefik-net` declared as `external: true`
- [ ] Backing services are **not** on `traefik-net`
- [ ] Backing services have **no** `ports:` section
- [ ] Container names follow `<project>-<service>` convention
- [ ] `.env.example` includes `HOMEBOX_DOMAIN` and any secrets

## Next Steps

- Use `homebox switch <project> dev --port <port>` to route your domain to your local dev server
- Use `homebox switch <project> pub` to route to the published container
- See the [bootstrap skill](claude-bootstrap-skill.md) for generating Dockerfile, CI/CD, and other scaffolding
