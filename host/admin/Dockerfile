# ---- Frontend build stage ----
FROM node:20-alpine AS frontend
WORKDIR /build
COPY frontend/package.json frontend/package-lock.json* /build/
RUN npm install --no-audit --no-fund --legacy-peer-deps
COPY frontend /build
RUN npm run build

# ---- cloudflared (browser "Connect with Cloudflare" login flow) ----
# We drive `cloudflared tunnel login` as a subprocess to run Cloudflare's own
# browser authorize→cert.pem transfer, then extract the embedded API token
# (see app/cf_login.py). Official static binary, copied into the runtime below.
FROM cloudflare/cloudflared:latest AS cloudflared

# ---- Python runtime ----
FROM python:3.12-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /srv

# curl/ca-certificates/git: base + cloning project repos.
# Docker CLI + compose plugin: the admin shells out to `docker compose` against
# the mounted socket to deploy projects (see app/deploy.py). Debian ships neither
# the v2 compose plugin nor a client-only docker package, so install both from
# Docker's official apt repo. We need only the CLIENT — the daemon is the host's,
# reached via the bind-mounted /var/run/docker.sock.
RUN apt-get update -qq && apt-get install -y -qq --no-install-recommends \
        curl ca-certificates git \
    && install -m 0755 -d /etc/apt/keyrings \
    && curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc \
    && chmod a+r /etc/apt/keyrings/docker.asc \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
        > /etc/apt/sources.list.d/docker.list \
    && apt-get update -qq \
    && apt-get install -y -qq --no-install-recommends docker-ce-cli docker-compose-plugin \
    && rm -rf /var/lib/apt/lists/*

# Nixpacks: infers a build (Dockerfile + image) for repos that ship neither a
# docker-compose file nor a Dockerfile. Single static binary.
RUN curl -fsSL https://nixpacks.com/install.sh | bash

COPY pyproject.toml /srv/pyproject.toml
RUN pip install --no-cache-dir \
    "fastapi>=0.115" \
    "uvicorn[standard]>=0.30" \
    "python-multipart>=0.0.9" \
    "pydantic>=2.8" \
    "pydantic-settings>=2.4" \
    "sqlalchemy[asyncio]>=2.0" \
    "asyncpg>=0.29" \
    "httpx>=0.27" \
    "cryptography>=43" \
    "bcrypt>=4.2" \
    "itsdangerous>=2.2" \
    "pyyaml>=6.0"

COPY --from=cloudflared /usr/local/bin/cloudflared /usr/local/bin/cloudflared

COPY app /srv/app
COPY --from=frontend /build/dist /srv/static_spa

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
