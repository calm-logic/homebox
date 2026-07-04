"""Single source of truth for the per-service, per-environment hostname scheme.

WILDCARD domain (shared by many projects) — project `box` on `x100.dev`:

    production   main UI   box.x100.dev
                 api       box-api.x100.dev
    dev          main UI   box--dev.x100.dev
                 api       box-api--dev.x100.dev

DEDICATED domain (one project owns it) — one entry host per environment, with
non-main public services PATH-proxied under it (see deploy._assemble_stack):

    production   main UI   infinitescroll.io
                 api       infinitescroll.io/api
    dev          main UI   dev.infinitescroll.io
                 api       dev.infinitescroll.io/api

Both schemes resolve through the domain's apex + wildcard CNAMEs at Cloudflare,
so no per-service records are needed in either mode.
"""

from .models import Domain, Environment, Project, Service


def host_label(project_name: str, subdomain_label: str, slug_suffix: str,
               *, dedicated: bool = False) -> str:
    """The left-hand label of a hostname. Empty string means the domain root
    itself (dedicated production main service)."""
    label = (subdomain_label or "").strip().lower()
    if dedicated:
        env_part = (slug_suffix or "").strip("-").lower()
        return "-".join(p for p in (label, env_part) if p)
    base = project_name.strip().lower()
    if label:
        base = f"{base}-{label}"
    return f"{base}{slug_suffix or ''}"


def full_host(project_name: str, subdomain_label: str, slug_suffix: str,
              domain_name: str, *, dedicated: bool = False) -> str:
    root = domain_name.strip(".").lower()
    hl = host_label(project_name, subdomain_label, slug_suffix, dedicated=dedicated)
    return f"{hl}.{root}" if hl else root


def service_host(project: Project, service: Service, env: Environment,
                 domain_name: str, *, dedicated: bool = False) -> str:
    """Full public hostname for a service in an environment."""
    return full_host(project.name, service.subdomain_label, env.slug_suffix,
                     domain_name, dedicated=dedicated)


def service_url(project: Project, service: Service, env: Environment,
                domain_name: str, *, dedicated: bool = False) -> str:
    return f"https://{service_host(project, service, env, domain_name, dedicated=dedicated)}"


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
