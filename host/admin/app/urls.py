"""Single source of truth for the per-service, per-environment hostname scheme.

For a project named `box` on the wildcard domain `x100.dev`:

    production   main UI   box.x100.dev
                 api       box-api.x100.dev
                 db        box-db.x100.dev
    dev          main UI   box--dev.x100.dev
                 api       box-api--dev.x100.dev

The label segment is the project name plus the service's subdomain_label (joined
with '-'), then the environment's slug_suffix ("" for production, "--dev" for
dev, "--<feature>" for a feature env), then the domain root.

With a wildcard primary domain (`*.x100.dev`) every derived host resolves
through the existing wildcard DNS + tunnel ingress, so no per-service Cloudflare
record is needed. Dedicated-domain projects need explicit records (handled by
the deploy path).
"""

from .models import Domain, Environment, Project, Service


def host_label(project_name: str, subdomain_label: str, slug_suffix: str) -> str:
    """The left-hand label of a hostname (everything before the domain root)."""
    base = project_name.strip().lower()
    label = (subdomain_label or "").strip().lower()
    if label:
        base = f"{base}-{label}"
    return f"{base}{slug_suffix or ''}"


def service_host(project: Project, service: Service, env: Environment, domain_name: str) -> str:
    """Full public hostname for a service in an environment."""
    return f"{host_label(project.name, service.subdomain_label, env.slug_suffix)}.{domain_name.strip('.').lower()}"


def service_url(project: Project, service: Service, env: Environment, domain_name: str) -> str:
    return f"https://{service_host(project, service, env, domain_name)}"


def stack_name(project: Project, env: Environment) -> str:
    """docker-compose project name for a (project, environment) stack. Prod and
    dev get distinct stacks so they coexist with separate volumes."""
    return f"homebox-proj-{project.name}-{env.name}".lower()


def pick_domain_name(project: Project, primary: Domain | None) -> str | None:
    """Resolve the domain root for a project: its own domain if set, else the
    primary wildcard. None means LAN-only (no public routing)."""
    if project.domain is not None:
        return project.domain.name
    if primary is not None:
        return primary.name
    return None
